FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src

RUN pip install --no-cache-dir "uv>=0.8,<0.13"

WORKDIR /app

# 의존성만 먼저 설치해 레이어 캐시를 활용
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN mkdir -p var/log var/tmp

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/version', timeout=3)" || exit 1

# 프록시 헤더(X-Forwarded-*)는 EBMS_FORWARDED_ALLOW_IPS(쉼표 구분, 기본 127.0.0.1)에서 온 요청만 믿습니다.
# 앞단 TLS 리버스 프록시의 주소(docker 브리지 게이트웨이 등)로 맞추세요(docs/deployment.md).
ENV EBMS_FORWARDED_ALLOW_IPS=127.0.0.1
CMD ["sh", "-c", "exec fastapi run --entrypoint ebms_server.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"${EBMS_FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
