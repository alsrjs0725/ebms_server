"""bmson(JSON) 차트: 차트로 등록, 메타데이터·사전 파일 판정, 기존 곡의 bmson을 차트로 옮기는 마이그레이션."""
import io
import json
import zipfile

from ebms_server import constant, db as db_module
from ebms_server.db import Database, chart_meta, kinds_from_charts

from test_auth import client  # noqa: F401  (곡마다 새 DB)

BMSON = {
    "version": "1.0.0",
    "info": {
        "title": "Song [ANOTHER]",
        "subtitle": "ANOTHER",
        "artist": "Composer / obj:someone",
        "genre": "TECHNO",
        "init_bpm": 150.0,
        "banner_image": "banner.png",
        "back_image": "img\\back.bmp",
        "preview_music": "preview.ogg",
    },
    "sound_channels": [{"name": "kick.wav", "notes": []}],
    "bga": {"bga_header": [{"id": 1, "name": "bga/movie.mp4"}], "bga_events": []},
}


def make_song(root, name="song", bmson=BMSON):
    song = root / name
    (song / "bga").mkdir(parents=True)
    (song / "img").mkdir()
    (song / "chart.bmson").write_bytes(json.dumps(bmson, ensure_ascii=False).encode())
    (song / "chart.bms").write_bytes(b"#TITLE Song\r\n#BPM 150\r\n#WAV01 snare.wav\r\n")
    for path in ("banner.jpg", "img/back.bmp", "preview.ogg", "kick.ogg", "snare.wav", "bga/movie.mp4"):
        (song / path).write_bytes(path.encode())
    return song


def chart_rows():
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT song_id, filename, md5, title_key, artist_key, bpm_key, genre_key FROM chart ORDER BY filename")
        return cur.fetchall()


def chunk_names():
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT data FROM chart_chunk ORDER BY id")
        names = []
        for (data,) in cur.fetchall():
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names += zf.namelist()
        return sorted(names)


def test_bmson_meta():
    assert chart_meta(json.dumps(BMSON).encode()) == {
        "title": "Song [ANOTHER]", "artist": "Composer / obj:someone", "bpm": "150", "genre": "TECHNO",
    }
    # BOM, 잘못된 JSON, info 없음
    assert chart_meta(b"\xef\xbb\xbf" + json.dumps(BMSON).encode())["title"] == "Song [ANOTHER]"
    assert chart_meta(b"{ not json") == {"title": "", "artist": "", "bpm": "", "genre": ""}
    assert chart_meta(b'{"version": "1.0.0"}') == {"title": "", "artist": "", "bpm": "", "genre": ""}


def test_bmson_file_kinds():
    paths = ["chart.bmson", "banner.jpg", "img/back.bmp", "preview.ogg", "kick.ogg", "bga/movie.mp4", "other.png"]
    kinds = kinds_from_charts(paths, {"chart.bmson": json.dumps(BMSON).encode()})
    assert kinds == {
        "chart.bmson": "pre",
        "banner.jpg": "pre",       # banner.png → 같은 이미지 형식
        "img/back.bmp": "pre",     # 역슬래시 경로
        "preview.ogg": "pre",
        "kick.ogg": "play",        # 키음 kick.wav → 확장자 대체
        "bga/movie.mp4": "play",
        "other.png": "play",
    }


def test_insert_song_registers_bmson(tmp_path, client):
    db = Database()
    result = db.insert_song(make_song(tmp_path))
    assert result["charts"] == 2
    rows = chart_rows()
    assert [(r[0], r[1]) for r in rows] == [(1, "chart.bms"), (1, "chart.bmson")]
    assert rows[1][3:] == ("song", "composer", "150", "techno")
    assert len([n for n in chunk_names() if n.endswith(".bmson")]) == 1
    kinds = {e["path"]: e["kind"] for e in db.get_song_files(1)}
    assert kinds["chart.bmson"] == "pre" and kinds["banner.jpg"] == "pre"


def test_backfill_chart_formats(tmp_path, client, monkeypatch):
    """.bmson을 차트로 보기 전에 넣은 곡의 bmson을 시작할 때 차트로 등록합니다."""
    db = Database()
    with monkeypatch.context() as m:
        m.setattr(constant, "BMS_FORMAT", db_module.LEGACY_CHART_FORMATS)
        db.insert_song(make_song(tmp_path))
    assert [r[1] for r in chart_rows()] == ["chart.bms"]

    assert db.backfill_chart_formats() == 1
    rows = chart_rows()
    assert [(r[0], r[1]) for r in rows] == [(1, "chart.bms"), (1, "chart.bmson")]
    assert rows[1][2] and rows[1][3] == "song"
    assert len([n for n in chunk_names() if n.endswith(".bmson")]) == 1
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT value FROM setting WHERE name = %s", (db_module.CHART_FORMATS_SETTING,))
        assert cur.fetchone()[0] == ",".join(constant.BMS_FORMAT)
    # 다음 시작 때는 하지 않음
    assert db.backfill_chart_formats() == 0
    assert len(chunk_names()) == 2


def test_backfill_chart_formats_skips_existing_chart(tmp_path, client, monkeypatch):
    """같은 bmson이 이미 다른 곡의 차트로 있으면 새로 넣지 않습니다."""
    db = Database()
    db.insert_song(make_song(tmp_path, "a"))
    with monkeypatch.context() as m:
        m.setattr(constant, "BMS_FORMAT", db_module.LEGACY_CHART_FORMATS)
        other = make_song(tmp_path, "b")
        (other / "chart.bms").write_bytes(b"#TITLE Other\r\n")
        db.insert_song(other)
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("DELETE FROM setting WHERE name = %s", (db_module.CHART_FORMATS_SETTING,))
        con.commit()
    assert db.backfill_chart_formats() == 0
    assert [(r[0], r[1]) for r in chart_rows()] == [(1, "chart.bms"), (2, "chart.bms"), (1, "chart.bmson")]
