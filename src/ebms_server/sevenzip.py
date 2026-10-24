"""7z 풀기. 서버에 7-Zip(`7z` 또는 `7zz`)이 있어야 합니다.

- rar처럼 통째로 폴더에 푼 뒤 곡 폴더를 찾습니다. 풀린 심볼릭 링크는 지웁니다.
- 7z는 파일 이름을 UTF-16으로 저장하므로 보통은 이름 인코딩을 고르지 않습니다. 다만 Linux에서 UTF-8이 아닌
  이름(cp932 등 바이트 그대로)을 넣은 7z는 7-Zip이 그 바이트를 그대로 되살려 풀므로, 그런 이름만 zip처럼
  7z 전체에서 한 인코딩을 골라 바꿉니다. 맞는 인코딩이 없으면 SevenZipError(대체하지 않음).
- 분할 압축(`.7z.001`…)은 받지 않습니다.
"""
import logging
import os
import pathlib
import shutil
import subprocess

from . import importer
from .rar import remove_symlinks

logger = logging.getLogger(__name__)

# 비우면 7z, 7zz 순서로 찾습니다.
SEVENZIP = os.environ.get("EBMS_7Z", "")


class SevenZipError(Exception):
    pass


def is_7z(name: str) -> bool:
    return pathlib.PurePath(name).suffix.lower() == ".7z"


def _binary() -> str | None:
    for name in [SEVENZIP] if SEVENZIP else ["7z", "7zz"]:
        if shutil.which(name):
            return name
    return None


def extract(path: pathlib.Path, dest: pathlib.Path) -> None:
    """path를 dest에 풉니다. 실패하면 SevenZipError."""
    binary = _binary()
    if binary is None:
        raise SevenZipError("서버에 7-Zip(7z)이 없어 7z를 풀 수 없습니다.")
    dest.mkdir(parents=True, exist_ok=True)
    # x: 경로 유지, -y: 모두 예, -p: 빈 암호(암호가 걸렸으면 묻지 않고 실패), -bso0 -bsp0: 진행 출력 끔,
    # -t7z: 7z만 받음(다른 형식을 확장자만 바꿔 올리지 않도록). 7-Zip은 절대 경로·..를 떼고, 밖을 가리키는 링크는 풀지 않습니다.
    cmd = [binary, "x", "-t7z", "-y", "-p", "-bso0", "-bsp0", f"-o{dest}", "--", str(path)]
    # 로캘이 UTF-8이 아니면 일본어·한국어 파일 이름이 깨집니다.
    env = {**os.environ, "LC_ALL": "C.UTF-8"}
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", env=env)
    message = " ".join((proc.stderr + proc.stdout).split())
    remove_symlinks(dest, "7z")
    # 1은 치명적이지 않은 경고(잠긴 파일 등)입니다.
    if proc.returncode not in (0, 1):
        raise SevenZipError(f"7z를 풀지 못했습니다(7-Zip {proc.returncode}): {message or '알 수 없는 오류'}")
    if proc.returncode == 1:
        logger.warning(f"7z warning[{path}]: {message}")
    _fix_names(dest)


# UTF-8로 풀리지 않는 이름에 시도할 인코딩(zip과 같고 utf-8만 뺌)
NAME_ENCODINGS = tuple(e for e in importer.ZIP_NAME_ENCODINGS if e != "utf-8")


def _fix_names(dest: pathlib.Path) -> None:
    """풀린 이름 중 UTF-8이 아닌 것을 7z 전체에서 고른 한 인코딩으로 풀어 UTF-8 이름으로 바꿉니다.
    맞는 인코딩이 없거나 바꾼 이름이 이미 있으면 SevenZipError."""
    bad = []  # (부모 폴더, 원래 이름 바이트). 안쪽부터 바꾸도록 os.walk(topdown=False) 순서
    for parent, dirs, files in os.walk(os.fsencode(dest), topdown=False):
        for raw in files + dirs:
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                bad.append((parent, raw))
    if not bad:
        return
    encoding, failures = importer.guess_name_encoding([raw for _, raw in bad], NAME_ENCODINGS)
    if encoding is None:
        raise SevenZipError("7z 파일 이름이 UTF-8이 아니고 인코딩도 알 수 없습니다(" + "; ".join(failures) + ")")
    logger.info(f"7z non-UTF-8 names decoded as {encoding}: {len(bad)}")
    for parent, raw in bad:
        new = raw.decode(encoding).encode("utf-8")
        if os.path.lexists(os.path.join(parent, new)):
            raise SevenZipError(f"7z 파일 이름을 {encoding}로 바꾸면 겹칩니다: {new.decode('utf-8')}")
        os.rename(os.path.join(parent, raw), os.path.join(parent, new))
