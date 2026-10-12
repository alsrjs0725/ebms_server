# EBMS Server

BMS 곡·차트를 저장하고 클라이언트에 배포하는 서버입니다. FastAPI + MySQL로 동작하며, Google·Discord 로그인과 다운로드 할당량(티켓, 월 사용량)을 관리합니다.

## 설치

### Docker (서버 + MySQL 올인원)

빌드된 이미지(`ghcr.io/alsrjs0725/ebms_server`, amd64·arm64)를 받아 실행합니다. 소스는 필요 없습니다.

```bash
mkdir ebms && cd ebms
base=https://raw.githubusercontent.com/alsrjs0725/ebms_server/release
curl -fsSLO $base/docker-compose.yml
curl -fsSL $base/.env.example -o .env   # 비밀번호 등 수정
docker compose up -d
```

- 이미지 태그: `latest`·`release`(정식), `<버전>`(예: `0.1.0`), `stage`(스테이지). `.env`의 `EBMS_IMAGE`로 고릅니다.
- 업데이트: `docker compose pull && docker compose up -d`
- 소스에서 직접 빌드하려면 저장소를 받은 뒤 `docker compose up -d --build`

- 서버: http://localhost:8000 (`EBMS_PORT`로 변경). 호스트의 `127.0.0.1`에만 열리며, 외부에는 TLS 리버스 프록시(https)로 공개해야 합니다([docs/deployment.md](docs/deployment.md#tls-리버스-프록시-필수)).
- 로그인(OAuth) 설정은 [docs/auth.md](docs/auth.md)를 보세요.

## 문서

| 문서 | 내용 |
| --- | --- |
| [docs/deployment.md](docs/deployment.md) | 배포·운영: 포트, 데이터 볼륨, 곡 등록, 데이터 마이그레이션 |
| [docs/auth.md](docs/auth.md) | 로그인 설정(Google, Discord), 웹 로그인, 클라이언트 로그인(PKCE) |
| [docs/download.md](docs/download.md) | 다운로드 정책: 사전/플레이 구분, 플레이 티켓, 월 사용량 |
| [docs/proxy.md](docs/proxy.md) | 캐싱 리버스 프록시(오라클 VM 등): 권한 확인·캐시·접속 제한, 설치 순서 |
| [docs/admin.md](docs/admin.md) | 관리자 페이지와 관리자 API |
| [docs/api.md](docs/api.md) | HTTP API 레퍼런스: 엔드포인트, 공통 헤더, 응답 형식 |
