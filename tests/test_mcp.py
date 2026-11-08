"""병합 충돌 판단용 MCP 서버(/api/mcp): 관리자 API 키 인증, 도구 목록, case 자료, 소리 조각, 추천 저장 테스트."""
import base64

from fastapi.testclient import TestClient

from ebms_server import mcp

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
    me = call(api, token, "whoami")
    assert me["role"] == "user"
    assert me["server"].startswith("http")
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
    assert names == {
        "whoami", "list_conflict_units", "get_conflict_case", "get_file_diff", "get_sound_clip", "submit_recommendation",
        "list_song_findings", "get_song", "compare_songs", "get_compare_result", "split_song", "merge_songs",
        "remove_charts", "restore_charts", "delete_song", "set_finding_status",
    }
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
    # 값이 없는 항목(쓰는 차트 없음, 고른 것·추천 없음)은 빠집니다.
    assert "used_by_old" not in item and "choice" not in item and "ai" not in item
    assert "compare" in item
    rest = call(api, key, "get_conflict_case", stage_id=sid, unit_id=unit["id"], offset=1)
    # 곡 정보는 첫 페이지에만
    assert "existing_songs" not in rest and "new_charts" not in rest
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


def test_case_time_budget(client, monkeypatch):
    # 비교 계산이 시간 상한을 넘으면 limit보다 적게 주고 next_offset으로 이어 받게 합니다(최소 1개).
    sid, unit = conflicted(client, monkeypatch, {"k.wav": b"new", "readme.txt": b"one\nTWO\n"})
    key = create_key(client)
    api = TestClient(client.app)
    monkeypatch.setattr(mcp, "CASE_SECONDS", 0.0)
    case = call(api, key, "get_conflict_case", stage_id=sid, unit_id=unit["id"], limit=50)
    assert len(case["items"]) == 1 and case["next_offset"] == 1 and case["total"] == 2
    rest = call(api, key, "get_conflict_case", stage_id=sid, unit_id=unit["id"], offset=case["next_offset"])
    assert len(rest["items"]) == 1 and rest["next_offset"] is None


def test_song_cleanup_tools(client, monkeypatch):
    """등록된 곡 정리: 곡 정보 → 나누기 → 합치기(되돌리기), 발견 닫기."""
    from ebms_server import render_index

    admin_login(client, monkeypatch)
    key = create_key(client)
    api = TestClient(client.app)
    upload(client, "mixed.zip", make_zip({
        "a.bms": b"#TITLE A\n#ARTIST x\n#WAV01 a.wav\n", "b.bms": b"#TITLE B\n#ARTIST y\n#WAV01 b.wav\n",
        "a.wav": b"a", "b.wav": b"b",
    }))
    song = call(api, key, "get_song", song_id=1, detail=True)
    assert [(c["path"], c["title"], c["artist"], c["sounds"]) for c in song["charts"]] == [("a.bms", "A", "x", 1), ("b.bms", "B", "y", 1)]
    assert song["other_files"] == ["a.wav", "b.wav"]
    # 기본은 짧은 응답(제목 표 + [경로, 표 번호, 키음 수])
    short = call(api, key, "get_song", song_id=1)
    assert short["titles"] == [["A", "x"], ["B", "y"]] and short["charts"] == [["a.bms", 0, 1], ["b.bms", 1, 1]]
    assert "other_files" not in short
    assert rpc(api, key, "tools/call", {"name": "get_song", "arguments": {"song_id": 9}})["result"]["isError"]

    assert call(api, key, "list_song_findings")["findings"] == []
    con = render_index.connect()
    render_index._put_finding(con, "mixed", 1, 0, None, {"groups": [["a.bms"], ["b.bms"]]})
    con.commit()
    con.close()
    found = call(api, key, "list_song_findings", kind="mixed")["findings"]
    assert [(f["kind"], f["song_a"]) for f in found] == [("mixed", 1)]
    brief = call(api, key, "list_song_findings", brief=True)["findings"]
    assert brief == [{"id": found[0]["id"], "kind": "mixed", "song_a": 1, "folder_a": found[0]["folder_a"],
                      "groups": [[1, "a.bms"], [1, "b.bms"]]}]

    split = call(api, key, "split_song", song_id=1, charts=["b.bms"], folder="[y] B")
    assert split["song_id"] == 2 and split["moved_files"] == ["b.wav"]
    # 나눈 곡의 발견은 닫혀 다음 색인 때 다시 검사합니다.
    assert call(api, key, "list_song_findings")["findings"] == []
    assert call(api, key, "get_song", song_id=2)["folder"] == "[y] B"
    bad = rpc(api, key, "tools/call", {"name": "split_song", "arguments": {"song_id": 1, "charts": ["a.bms"]}})["result"]
    assert bad["isError"] and "400" in bad["content"][0]["text"]

    merged = call(api, key, "merge_songs", song_id=1, source=2)
    assert merged["merged_from"] == 2 and merged["subfolder"] is None
    assert [c[0] for c in call(api, key, "get_song", song_id=1)["charts"]] == ["a.bms", "b.bms"]

    con = render_index.connect()
    render_index._put_finding(con, "dup", 1, 3, 1.0, {})
    con.commit()
    fid = con.execute("SELECT id FROM finding").fetchone()[0]
    con.close()
    assert call(api, key, "set_finding_status", finding_id=fid, status="dismissed") == {"id": fid, "status": "dismissed"}
    assert call(api, key, "list_song_findings")["findings"] == []


def test_remove_and_delete_tools(client, monkeypatch, caplog):
    """살릴 수 없는 차트는 빼고, 곡 전체는 제외 → 삭제."""
    admin_login(client, monkeypatch)
    key = create_key(client)
    api = TestClient(client.app)
    upload(client, "q.zip", make_zip({"q.bms": b"#TITLE Q\n#WAV01 01.wav\n", "o.bms": b"#TITLE O\n#WAV01 01.wav\n", "01.wav": b"q"}))
    dry = call(api, key, "split_song", song_id=1, charts=["o.bms"], dry_run=True)
    assert dry["shared_only"] is True and call(api, key, "get_song", song_id=1)["charts"][1][0] == "q.bms"
    with caplog.at_level("INFO", logger="ebms_server.audit"):
        removed = call(api, key, "remove_charts", song_id=1, charts=["O.BMS"], reason="다른 곡 키음")
    # 사유와 뺀 파일이 접속기록에 남습니다.
    assert any("action=mcp.remove_charts" in r.message and "removed_files=o.bms" in r.message and "다른 곡 키음" in r.message
               for r in caplog.records)
    assert removed["removed_files"] == ["o.bms"]
    assert [c[0] for c in call(api, key, "get_song", song_id=1)["charts"]] == ["q.bms"]
    # 잘못 뺀 차트는 원본 없이 되살립니다.
    restored = call(api, key, "restore_charts", song_id=1, charts=removed["detached"], reason="같은 곡 변형")
    assert restored == {"attached": removed["detached"], "added_files": ["o.bms"]}
    assert [c[0] for c in call(api, key, "get_song", song_id=1)["charts"]] == ["o.bms", "q.bms"]
    assert client.get("/api/admin/songs/1/file", params={"path": "o.bms"}).content == b"#TITLE O\n#WAV01 01.wav\n"
    # 이미 붙은 차트는 400
    again = rpc(api, key, "tools/call", {"name": "restore_charts", "arguments": {"song_id": 1, "charts": removed["detached"], "reason": ""}})
    assert again["result"]["isError"]
    call(api, key, "remove_charts", song_id=1, charts=["o.bms"], reason="다시")

    assert call(api, key, "delete_song", song_id=1, reason="깨짐")["level"] == "unused"
    assert client.get("/api/admin/songs/deleted").json()["items"][0]["purged_at"] is None
    assert call(api, key, "delete_song", song_id=1, reason="깨짐", level="deleted")["purged"] is True
    assert client.get("/api/admin/songs/deleted").json()["items"][0]["purged_at"]
    bad = rpc(api, key, "tools/call", {"name": "delete_song", "arguments": {"song_id": 1, "reason": "", "level": "x"}})
    assert bad["result"]["isError"]
