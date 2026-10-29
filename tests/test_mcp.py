"""병합 충돌 판단용 MCP 서버(/api/mcp): 관리자 API 키 인증, 도구 목록, case 자료, 소리 조각, 추천 저장 테스트."""
import base64

from fastapi.testclient import TestClient

from test_api_keys import bearer, create_key
from test_auth import client  # noqa: F401  (fixture 재사용)
from test_client_auth import client_login
from test_conflicts import _media, base, conflicted, needs_ffmpeg
from test_import import make_zip, stage, upload
from test_quota import admin_login
from test_staging import commit_stage, units


def rpc(api, key, method, params=None, msg_id=1):
    r = api.post("/api/mcp", headers=bearer(key), json={"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
    assert r.status_code == 200, r.text
    return r.json()


def call(api, key, name, **arguments):
    result = rpc(api, key, "tools/call", {"name": name, "arguments": arguments})["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def test_requires_bearer(client, monkeypatch):
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    api = TestClient(client.app)
    assert api.post("/api/mcp", json=body).status_code == 401
    # 웹 세션(쿠키)으로는 못 씁니다(다른 사이트가 대신 부르지 못하게).
    admin_login(client, monkeypatch)
    assert client.post("/api/mcp", json=body).status_code == 401
    key = create_key(client)
    assert api.post("/api/mcp", headers=bearer(key + "x"), json=body).status_code == 401
    assert rpc(api, key, "ping") == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_user_sees_only_user_tools(client, monkeypatch):
    token, _ = client_login(client, monkeypatch)
    api = TestClient(client.app)
    tools = {t["name"] for t in rpc(api, token, "tools/list")["result"]["tools"]}
    assert tools == {"whoami"}
    assert call(api, token, "whoami")["role"] == "user"
    denied = rpc(api, token, "tools/call", {"name": "list_conflict_units", "arguments": {}})
    assert denied["error"]["code"] == -32003


def test_initialize_and_tools(client, monkeypatch):
    admin_login(client, monkeypatch)
    key = create_key(client)
    api = TestClient(client.app)
    init = rpc(api, key, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}})["result"]
    assert init["protocolVersion"] == "2025-03-26"
    assert "tools" in init["capabilities"] and "unsure" in init["instructions"]
    # 알림은 답하지 않습니다(202).
    r = api.post("/api/mcp", headers=bearer(key), json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202
    names = {t["name"] for t in rpc(api, key, "tools/list")["result"]["tools"]}
    assert names == {"whoami", "list_conflict_units", "get_conflict_case", "get_file_diff", "get_sound_clip", "submit_recommendation"}
    assert call(api, key, "whoami")["role"] == "admin"
    assert rpc(api, key, "nope")["error"]["code"] == -32601
    assert rpc(api, key, "tools/call", {"name": "nope"})["error"]["code"] == -32602
    assert api.get("/api/mcp", headers=bearer(key)).status_code == 405


def test_case_and_recommendation(client, monkeypatch):
    sid, unit = conflicted(client, monkeypatch, {"k.wav": b"new", "readme.txt": b"one\nTWO\n", "m.wav": b"m"})
    key = create_key(client)
    api = TestClient(client.app)

    units = call(api, key, "list_conflict_units")["units"]
    assert [(u["stage_id"], u["unit_id"], u["conflicts"], u["recommended"]) for u in units] == [(sid, unit["id"], 2, 0)]

    case = call(api, key, "get_conflict_case", stage_id=sid, unit_id=unit["id"], limit=1)
    assert case["total"] == 2 and case["next_offset"] == 1
    assert case["existing_songs"]["1"]["charts"] == [{"path": "a.bms", "title": "A", "artist": ""}]
    assert case["new_charts"] == [{"path": "a.bms", "title": "A", "artist": "", "status": "same_as_registered"}]
    item = case["items"][0]
    assert item["path"] == "k.wav" and item["kind"] == "sound"
    assert item["used_by_old"] == [] and item["used_by_new"] == []
    assert "compare" in item
    rest = call(api, key, "get_conflict_case", stage_id=sid, unit_id=unit["id"], offset=1)
    assert rest["next_offset"] is None and "+TWO" in rest["items"][0]["compare"]["diff_head"]
    diff = call(api, key, "get_file_diff", stage_id=sid, unit_id=unit["id"], path="readme.txt")
    assert "-two" in diff["diff"] and diff["truncated"] is False

    # 충돌 파일이 아니거나 고를 수 없는 값은 거부
    bad = rpc(api, key, "tools/call", {"name": "submit_recommendation", "arguments": {
        "stage_id": sid, "unit_id": unit["id"], "items": [{"path": "m.wav", "choice": "old"}]}})["result"]
    assert bad["isError"]
    bad = rpc(api, key, "tools/call", {"name": "submit_recommendation", "arguments": {
        "stage_id": sid, "unit_id": unit["id"], "items": [{"path": "k.wav", "choice": "maybe"}]}})["result"]
    assert bad["isError"]

    saved = call(api, key, "submit_recommendation", stage_id=sid, unit_id=unit["id"], model="flash",
                 summary="같은 곡", split_recommended=False, issues=[],
                 items=[{"path": "k.wav", "choice": "new", "confidence": 0.9, "reason": "품질 좋음"},
                        {"path": "readme.txt", "choice": "unsure"}])
    assert saved == {"saved": 2, "recommended": 2, "conflicts": 2, "missing": []}
    assert call(api, key, "list_conflict_units")["units"] == []
    assert len(call(api, key, "list_conflict_units", pending_only=False)["units"]) == 1

    # 추천은 따로 저장되고, 관리자가 고른 것(choice)은 그대로입니다.
    listing = client.get(base(sid, unit)).json()
    items = {i["path"]: i for i in listing["items"]}
    assert items["k.wav"]["choice"] is None
    assert items["k.wav"]["ai"]["choice"] == "new" and items["k.wav"]["ai"]["model"] == "flash"
    assert items["readme.txt"]["ai"]["choice"] == "unsure"
    assert listing["ai"]["summary"] == "같은 곡" and listing["ai"]["split_recommended"] is False


@needs_ffmpeg
def test_sound_clip(client, monkeypatch, tmp_path):
    # 기존·새 키음을 같은 구간으로 잘라 오디오(mp3)와 스펙트로그램(png)으로 줍니다.
    admin_login(client, monkeypatch)
    old = _media(tmp_path, "old.wav", ["-f", "lavfi", "-i", "sine=f=440:d=3"])
    new = _media(tmp_path, "new.wav", ["-f", "lavfi", "-i", "sine=f=880:d=3"])
    upload(client, "base.zip", make_zip({"a.bms": b"#TITLE A\n", "k.wav": old, "r.txt": b"a\n"}))
    job = stage(client, "pack.zip", make_zip({"p/s/a.bms": b"#TITLE A\n", "p/s/k.wav": new, "p/s/r.txt": b"b\n"}))
    sid = job["stage_id"]
    commit_stage(client, sid)
    unit = units(client, sid)["pack/p/s"]
    key = create_key(client)
    api = TestClient(client.app)

    result = rpc(api, key, "tools/call", {"name": "get_sound_clip", "arguments": {
        "stage_id": sid, "unit_id": unit["id"], "path": "k.wav", "start": 1, "seconds": 1}})["result"]
    assert not result.get("isError"), result
    kinds = [c["type"] for c in result["content"]]
    assert kinds == ["text", "text", "audio", "image", "text", "audio", "image"]
    audio = [c for c in result["content"] if c["type"] == "audio"]
    assert all(c["mimeType"] == "audio/mpeg" and base64.b64decode(c["data"])[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3") for c in audio)
    images = [c for c in result["content"] if c["type"] == "image"]
    assert all(base64.b64decode(c["data"])[:8] == b"\x89PNG\r\n\x1a\n" for c in images)

    # 스펙트로그램만
    only = rpc(api, key, "tools/call", {"name": "get_sound_clip", "arguments": {
        "stage_id": sid, "unit_id": unit["id"], "path": "k.wav", "audio": False}})["result"]
    assert [c["type"] for c in only["content"]] == ["text", "text", "image", "text", "image"]
    # 키음이 아닌 충돌은 거부
    bad = rpc(api, key, "tools/call", {"name": "get_sound_clip", "arguments": {
        "stage_id": sid, "unit_id": unit["id"], "path": "r.txt"}})["result"]
    assert bad["isError"]
