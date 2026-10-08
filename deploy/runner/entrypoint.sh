#!/usr/bin/env bash
set -euo pipefail
cd /actions-runner

# 등록 정보(.runner)는 볼륨에 남으므로 토큰은 처음 한 번만 필요
if [ ! -f .runner ]; then
  : "${RUNNER_TOKEN:?처음 등록에는 RUNNER_TOKEN이 필요합니다. docs/deployment.md를 보세요.}"
  ./config.sh --unattended --replace \
    --url "$RUNNER_URL" --token "$RUNNER_TOKEN" \
    --name "$RUNNER_NAME" --labels "$RUNNER_LABELS"
fi

exec ./run.sh
