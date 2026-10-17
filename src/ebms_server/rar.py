"""RAR(분할 압축 포함) 풀기. 서버에 `unrar`(RARLAB)가 있어야 합니다.

- 볼륨 이름은 새 방식 `이름.part1.rar`·`이름.part2.rar`…와 옛 방식 `이름.rar`·`이름.r00`·`이름.r01`…을 받습니다.
  같은 묶음의 볼륨은 같은 폴더에 있어야 하고, unrar에는 첫 볼륨을 넘깁니다.
- 통째로 폴더에 푼 뒤 곡 폴더를 찾습니다(zip처럼 곡 하나씩 풀지 않음). 풀린 심볼릭 링크는 지웁니다.
"""
import logging
import os
import pathlib
import re
import shutil
import subprocess

logger = logging.getLogger(__name__)

UNRAR = os.environ.get("EBMS_UNRAR", "unrar")

_PART = re.compile(r"^(.*)\.part(\d+)\.rar$", re.IGNORECASE)
_OLD = re.compile(r"^(.*)\.(rar|r\d\d)$", re.IGNORECASE)


class RarError(Exception):
    pass


def volume(name: str) -> tuple[tuple[str, str], int] | None:
    """RAR 볼륨이면 (묶음 키, 순서). 키는 (방식, 소문자 이름)이고 첫 볼륨의 순서가 가장 작습니다. 아니면 None."""
    m = _PART.match(name)
    if m:
        return ("part", m.group(1).lower()), int(m.group(2))
    m = _OLD.match(name)
    if m:
        ext = m.group(2).lower()
        return ("old", m.group(1).lower()), -1 if ext == "rar" else int(ext[1:])
    return None


def stem(name: str) -> str:
    """볼륨 이름에서 확장자(.partN.rar, .rar, .rNN)를 뗀 이름."""
    m = _PART.match(name) or _OLD.match(name)
    return m.group(1) if m else pathlib.PurePath(name).stem


def is_volume(name: str) -> bool:
    return volume(name) is not None


def volume_sets(names: list[str]) -> dict[tuple[str, str], list[str]]:
    """이름을 볼륨 묶음별로 나눕니다(묶음 안은 볼륨 순서대로). 볼륨이 아닌 이름은 버립니다."""
    sets: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for name in names:
        v = volume(name)
        if v is not None:
            sets.setdefault(v[0], []).append((v[1], name))
    return {key: [n for _, n in sorted(items)] for key, items in sorted(sets.items())}


def first_volume(names: list[str]) -> str:
    """같은 묶음의 볼륨 이름 중 첫 볼륨. 첫 볼륨이 없으면 RarError."""
    (style, _), order, name = min((*volume(n), n) for n in names)
    if order != (1 if style == "part" else -1):
        raise RarError(f"첫 번째 볼륨이 없습니다({name}부터 있음).")
    return name


def extract(path: pathlib.Path, dest: pathlib.Path) -> None:
    """첫 볼륨 path를 dest에 풉니다(나머지 볼륨은 같은 폴더에서 찾음). 실패하면 RarError."""
    if shutil.which(UNRAR) is None:
        raise RarError("서버에 unrar가 없어 rar를 풀 수 없습니다.")
    dest.mkdir(parents=True, exist_ok=True)
    # x: 경로 유지, -o+: 덮어쓰기, -p-: 암호 묻지 않음, -y: 모두 예, -ai: 파일 속성 무시, -idq: 조용히
    # 볼륨이 빠지면 묻지 않고 실패하도록 입력을 닫습니다.
    cmd = [UNRAR, "x", "-o+", "-p-", "-y", "-ai", "-idq", "--", str(path), str(dest) + os.sep]
    # 로캘이 UTF-8이 아니면 unrar가 일본어·한국어 파일 이름을 ?로 바꿉니다.
    env = {**os.environ, "LC_ALL": "C.UTF-8"}
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", env=env)
    message = " ".join((proc.stderr + proc.stdout).split())
    _remove_symlinks(dest)
    # 1은 치명적이지 않은 경고(위험한 링크를 건너뜀 등)입니다.
    if proc.returncode not in (0, 1):
        raise RarError(f"rar를 풀지 못했습니다(unrar {proc.returncode}): {message or '알 수 없는 오류'}")
    if proc.returncode == 1:
        logger.warning(f"unrar warning[{path}]: {message}")


def _remove_symlinks(root: pathlib.Path) -> None:
    """풀린 심볼릭 링크를 지웁니다(곡 밖의 파일을 가리키지 않도록)."""
    for dirpath, dirnames, filenames in os.walk(root):
        links = [n for n in dirnames + filenames if os.path.islink(os.path.join(dirpath, n))]
        dirnames[:] = [d for d in dirnames if d not in links]
        for name in links:
            logger.warning(f"rar: removed symlink[{os.path.join(dirpath, name)}]")
            os.unlink(os.path.join(dirpath, name))
