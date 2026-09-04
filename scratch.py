import os
import sys
import pathlib
import zipfile
import logging

from ebms_server import constant

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("scratch_migration")


def flatten_chart_chunk(zip_path: pathlib.Path) -> bool:
    """기존 차트 청크 압축파일 내부의 경로를 파일명으로 평탄화 (ZIP_STORED)"""
    zip_path = pathlib.Path(zip_path).resolve()
    if not zip_path.exists():
        logger.warning(f"File not found: {zip_path}")
        return False

    bak_path = zip_path.with_suffix(".zip.bak")
    if bak_path.exists():
        logger.error(f"Backup file already exists: {bak_path}. Aborting to avoid overwrite.")
        return False

    # 1. 파일 이름 변경
    zip_path.rename(bak_path)

    try:
        with zipfile.ZipFile(bak_path, "r") as old_zf:
            infolist = old_zf.infolist()

            # 2. 원래 파일명으로 신규 평탄화 압축파일 생성
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as new_zf:
                for info in infolist:
                    clean_name = pathlib.PurePath(info.filename.replace("\\", "/")).name
                    if not clean_name:
                        continue  # 디렉터리 항목은 스킵

                    data = old_zf.read(info.filename)

                    new_info = zipfile.ZipInfo(clean_name, date_time=info.date_time)
                    new_info.create_system = 0
                    new_info.external_attr = 0
                    new_info.compress_type = zipfile.ZIP_STORED

                    new_zf.writestr(new_info, data)

        # 무결성 검증
        with zipfile.ZipFile(zip_path, "r") as test_zf:
            bad_file = test_zf.testzip()
            if bad_file:
                raise RuntimeError(f"Corrupted zip generated: bad file {bad_file}")

        # 3. 성공 시 백업(기존) 파일 삭제
        bak_path.unlink()
        logger.info(f"Flattened chart chunk: {zip_path.name}")
        return True

    except Exception as e:
        logger.error(f"Failed to flatten chart chunk {zip_path.name}: {e}")
        # 롤백
        if zip_path.exists():
            zip_path.unlink()
        bak_path.rename(zip_path)
        raise


def flatten_song_zip(zip_path: pathlib.Path) -> bool:
    """기존 곡 에셋 압축파일의 최상위 루트 디렉터리 접두사 평탄화 (ZIP_DEFLATED)"""
    zip_path = pathlib.Path(zip_path).resolve()
    if not zip_path.exists():
        logger.warning(f"File not found: {zip_path}")
        return False

    bak_path = zip_path.with_suffix(".zip.bak")
    if bak_path.exists():
        logger.error(f"Backup file already exists: {bak_path}. Aborting to avoid overwrite.")
        return False

    zip_path.rename(bak_path)

    try:
        with zipfile.ZipFile(bak_path, "r") as old_zf:
            infolist = old_zf.infolist()
            if not infolist:
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as new_zf:
                    pass
                bak_path.unlink()
                return True

            # 최상위 공통 루트 폴더 감지
            normalized_names = [info.filename.replace("\\", "/").lstrip("/") for info in infolist]

            first_parts = set()
            for name in normalized_names:
                parts = name.split("/", 1)
                first_parts.add(parts[0])

            has_single_root = len(first_parts) == 1 and any("/" in name for name in normalized_names)
            root_prefix = list(first_parts)[0] + "/" if has_single_root else None

            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as new_zf:
                for info in infolist:
                    norm_name = info.filename.replace("\\", "/").lstrip("/")
                    
                    if root_prefix:
                        if norm_name == root_prefix.rstrip("/"):
                            continue
                        if norm_name.startswith(root_prefix):
                            new_name = norm_name[len(root_prefix):]
                        else:
                            new_name = norm_name
                    else:
                        new_name = norm_name

                    if not new_name:
                        continue

                    is_dir = info.is_dir() or new_name.endswith("/")
                    if is_dir and not new_name.endswith("/"):
                        new_name += "/"

                    new_info = zipfile.ZipInfo(new_name, date_time=info.date_time)
                    new_info.create_system = 0
                    new_info.external_attr = 0

                    if is_dir:
                        new_zf.writestr(new_info, b"")
                    else:
                        new_info.compress_type = zipfile.ZIP_DEFLATED
                        data = old_zf.read(info.filename)
                        new_zf.writestr(new_info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)

        with zipfile.ZipFile(zip_path, "r") as test_zf:
            bad_file = test_zf.testzip()
            if bad_file:
                raise RuntimeError(f"Corrupted zip generated: bad file {bad_file}")

        bak_path.unlink()
        logger.info(f"Flattened song zip: {zip_path.name}")
        return True

    except Exception as e:
        logger.error(f"Failed to flatten song zip {zip_path.name}: {e}")
        if zip_path.exists():
            zip_path.unlink()
        bak_path.rename(zip_path)
        raise


def run_migration():
    chart_dir = constant.CHART_DATA_DIR
    song_dir = constant.SONG_DATA_DIR

    logger.info("=== Starting Archive Flattening Migration ===")

    chart_files = sorted(chart_dir.glob("chart_chunk_*.zip"))
    logger.info(f"Found {len(chart_files)} chart chunk files in {chart_dir}")
    for cf in chart_files:
        flatten_chart_chunk(cf)

    song_files = sorted([f for f in song_dir.glob("*.zip") if not f.name.endswith(".bak")])
    logger.info(f"Found {len(song_files)} song zip files in {song_dir}")
    for sf in song_files:
        flatten_song_zip(sf)

    logger.info("=== Archive Flattening Migration Finished ===")


if __name__ == "__main__":
    run_migration()
