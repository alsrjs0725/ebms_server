"""렌더 색인 프로세스 진입점(render_index._Run이 띄움): python -m ebms_server.render_index_worker 분

웹 서버와 GIL을 나누지 않도록 따로 띄운 프로세스에서, 정한 코어(RENDER_INDEX_CPUS)에 붙여 낮은 우선순위로 색인합니다
(ffmpeg도 물려받음). 로그는 "수준|로거|메시지" 줄로 stdout에 쓰고, 끝에 할 일이 남았는지를 "INFO|result|1/0"으로 씁니다.
"""
import logging
import os
import sys

from . import render_index


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)s|%(name)s|%(message)s", force=True)
    minutes = float(sys.argv[1])
    left = False
    try:
        cpus = render_index._cpus()
        if cpus:
            os.sched_setaffinity(0, cpus)
        os.nice(render_index.NICE)
        render_index.logger.info(f"render index process {os.getpid()}: cpus {sorted(cpus) if cpus else 'any'}, {minutes} min")
        left = render_index.run(minutes)
    except Exception:
        render_index.logger.exception("render index process failed")
    print(f"INFO|{render_index.RESULT}|{1 if left else 0}", flush=True)


if __name__ == "__main__":
    main()
