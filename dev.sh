#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec uv run fastapi dev \
    --entrypoint ebms_server.main:app \
    --host 127.0.0.1 \
    --port 1234 \
    --proxy-headers \
    --forwarded-allow-ips 127.0.0.1
