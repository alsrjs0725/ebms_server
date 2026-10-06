"""이전 버전 데이터를 현재 형식으로 바꾸는 일회성 스크립트입니다.

    python -m ebms_server.migrate

- 테이블에 없는 컬럼 추가, 곡 매니페스트 채우기 (서버 시작 시에도 자동 실행)
- chart chunk 안의 항목 이름을 {sha256}{ext}로 변경. chunk 해시가 바뀌므로 클라이언트는 다시 받습니다.
"""
import logging

from .db import Database


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Database().migrate_chart_chunk_names()


if __name__ == "__main__":
    main()
