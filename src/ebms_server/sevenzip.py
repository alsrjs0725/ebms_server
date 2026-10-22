"""7z 풀기. 서버에 7-Zip(`7z` 또는 `7zz`)이 있어야 합니다.

- rar처럼 통째로 폴더에 푼 뒤 곡 폴더를 찾습니다. 풀린 심볼릭 링크는 지웁니다.
- 7z는 파일 이름을 UTF-16으로 저장하므로 zip처럼 이름 인코딩을 고르지 않습니다.
- 분할 압축(`.7z.001`…)은 받지 않습니다.
"""
import logging
import os
import pathlib
import shutil
import subprocess

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
