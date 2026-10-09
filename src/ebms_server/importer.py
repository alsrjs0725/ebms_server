"""곡 임포트. 폴더 트리에서 차트 파일이 있는 폴더를 곡 하나로 보고 DB에 등록합니다.

- 서버 시작 시와 관리자 페이지의 "var/tmp 가져오기"는 `var/tmp/`를 훑고, 등록한 곡 폴더는 지웁니다.
- 관리자 페이지에서 올린 zip은 `var/import/` 아래 임시 폴더에 풀어 등록한 뒤 지웁니다.
"""
import logging
import os
import pathlib
import shutil
import uuid
import zipfile
from typing import BinaryIO

from . import constant
from .db import Database

logger = logging.getLogger(__name__)

# zip 항목 이름에 UTF-8 플래그가 없을 때 시도할 인코딩. 일본 BMS 패키지는 대부분 cp932입니다.
ZIP_NAME_ENCODINGS = ("utf-8", "cp932", "cp949")


def find_song_dirs(root: pathlib.Path) -> list[pathlib.Path]:
    """차트 파일이 바로 아래 있는 폴더 목록. 곡 폴더 안의 하위 폴더(bga 등)는 곡의 일부로 보고 더 들어가지 않습니다."""
    songs = []
    for dirpath, dirnames, filenames in os.walk(root):
        if any(pathlib.Path(f).suffix.lower() in constant.BMS_FORMAT for f in filenames):
            songs.append(pathlib.Path(dirpath))
            dirnames.clear()
        else:
            dirnames.sort()
    return songs


def import_tree(root: pathlib.Path, remove: bool) -> list[dict]:
    """root의 하위 폴더에서 곡 폴더를 모두 등록합니다(root 바로 아래 파일은 보지 않습니다).
    곡마다 결과를 반환하고, 한 곡이 실패해도 나머지는 계속합니다. remove면 등록에 성공한 곡 폴더를 지웁니다.
    """
    root.mkdir(parents=True, exist_ok=True)
    song_dirs = []
    for child in sorted(root.iterdir()):
        if child.is_dir():
            song_dirs.extend(find_song_dirs(child))
    results = []
    for song_dir in song_dirs:
        folder = song_dir.relative_to(root).as_posix()
        try:
            info = Database().insert_song(song_dir, remove=remove)
        except Exception as e:
            logger.exception(f"import failed[{song_dir}]")
            results.append({"folder": folder, "error": str(e) or type(e).__name__})
            continue
        if info is None:
            results.append({"folder": folder, "error": "등록하지 못했습니다(곡 크기가 DB 패킷 한도를 넘었을 수 있습니다). 서버 로그를 보세요."})
        else:
            results.append({"folder": folder, **info})
    return results


def import_tmp() -> list[dict]:
    """`var/tmp/` 아래 곡을 등록하고 등록한 곡 폴더는 지웁니다."""
    return import_tree(constant.TMP_DIR, remove=True)


def _entry_name(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x800:
        return info.filename
    # UTF-8 플래그가 없으면 zipfile은 cp437로 읽으므로 원래 바이트로 되돌려 다시 해석합니다.
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for encoding in ZIP_NAME_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return info.filename


def _safe_parts(name: str) -> list[str] | None:
    """zip 항목 이름을 경로 조각으로 나눕니다. 절대 경로나 상위 폴더(..)를 가리키면 None."""
    name = name.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return parts


def extract_zip(src: BinaryIO, dest: pathlib.Path) -> int:
    """zip을 dest에 풉니다. 풀어낸 파일 수를 반환합니다. 위험한 경로의 항목은 건너뜁니다."""
    count = 0
    with zipfile.ZipFile(src) as zf:
        for info in zf.infolist():
            parts = _safe_parts(_entry_name(info))
            if not parts:
                logger.warning(f"extract_zip: skipped unsafe entry[{info.filename!r}]")
                continue
            target = dest.joinpath(*parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as fin, open(target, "wb") as fout:
                shutil.copyfileobj(fin, fout, constant.BLOB_READ_SIZE)
            count += 1
    return count


def import_zip(src: BinaryIO, filename: str) -> list[dict]:
    """올린 zip 하나를 풀어 곡을 등록합니다. zip 최상위에 차트가 있으면 zip 이름을 곡 폴더명으로 씁니다."""
    stem = pathlib.PurePath(filename.replace("\\", "/")).stem
    if stem in ("", ".", ".."):
        stem = "song"
    work = constant.IMPORT_DIR / uuid.uuid4().hex
    try:
        root = work / stem
        root.mkdir(parents=True)
        extract_zip(src, root)
        return import_tree(work, remove=False)
    finally:
        shutil.rmtree(work, ignore_errors=True)
