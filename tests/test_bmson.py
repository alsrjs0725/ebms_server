"""bmson(JSON) 차트: 차트로 등록, 메타데이터·사전 파일 판정."""
import io
import json
import zipfile

from ebms_server import db as db_module
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
        cur.execute("SELECT song_id, filename, md5, title_key, artist_key, bpm_key, genre_key FROM chart ORDER BY filename, song_id")
        return cur.fetchall()


def chunk_names():
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT id FROM chart_chunk ORDER BY id")
        names = []
        for (chunk_id,) in cur.fetchall():
            with zipfile.ZipFile(io.BytesIO(db_module.Database().get_chart_chunk_data(chunk_id))) as zf:
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
