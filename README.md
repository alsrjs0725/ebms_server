# EBMS Server

## Docker (서버 + MySQL 올인원)

```bash
cp .env.example .env   # 비밀번호 등 수정
docker compose up -d --build
```

- 서버: http://localhost:8000 (`EBMS_PORT`로 변경)

### 포트

| 포트 | 서비스 | 외부 공개 | 비고 |
| --- | --- | --- | --- |
| 8000/tcp (`EBMS_PORT`) | EBMS 서버 (HTTP) | 필요 | 방화벽/리버스 프록시에서 열어야 하는 유일한 포트 |
| 3306/tcp | MySQL | 불필요 | 호스트에 바인딩하지 않음. compose 내부 네트워크에서 `ebms` 컨테이너만 접근 |

- 곡/차트 데이터는 MySQL(`mysql-data` 볼륨)에 BLOB으로 저장되며, 테이블은 서버가 시작할 때 자동 생성됩니다.
- 서버 로그와 임포트 대기 폴더(`var/log`, `var/tmp`)는 `ebms-var` 볼륨에 유지됩니다.

## 로그인 설정 (Google, Discord)

계정은 내부 UUID로 관리하고, 한 계정에 Google·Discord를 함께 연결할 수 있습니다. `.env`에 아래 값을 넣고 `docker compose up -d`로 다시 올리면 됩니다. 키를 넣지 않은 OAuth는 로그인 페이지에 나오지 않습니다.

| 변수 | 설명 |
| --- | --- |
| `EBMS_PUBLIC_URL` | 브라우저가 접속하는 서버 주소 (예: `https://ebms.example.com`). `https://`면 쿠키에 `Secure`가 붙습니다 |
| `EBMS_SECRET_KEY` | 쿠키 서명용 임의 문자열. `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `EBMS_GOOGLE_CLIENT_ID`, `EBMS_GOOGLE_CLIENT_SECRET` | Google OAuth 클라이언트 |
| `EBMS_DISCORD_CLIENT_ID`, `EBMS_DISCORD_CLIENT_SECRET` | Discord OAuth 앱 |
| `EBMS_ADMIN_EMAILS` | 이 이메일(OAuth가 확인한 것만)로 로그인하면 관리자로 지정. 쉼표로 구분 |

### Google

1. [Google Cloud Console](https://console.cloud.google.com/apis/credentials) → 사용자 인증 정보 만들기 → **OAuth 클라이언트 ID** → 애플리케이션 유형 **웹 애플리케이션**
2. 승인된 리디렉션 URI: `<EBMS_PUBLIC_URL>/auth/google/callback`
3. OAuth 동의 화면의 범위는 `openid`, `email`, `profile`이면 충분합니다.
4. 발급된 클라이언트 ID/보안 비밀을 `EBMS_GOOGLE_CLIENT_ID`/`EBMS_GOOGLE_CLIENT_SECRET`에 넣습니다.

### Discord

1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application → **OAuth2**
2. Redirects에 `<EBMS_PUBLIC_URL>/auth/discord/callback` 추가
3. Client ID/Client Secret을 `EBMS_DISCORD_CLIENT_ID`/`EBMS_DISCORD_CLIENT_SECRET`에 넣습니다. 요청 범위는 `identify email`입니다.

### 웹 페이지

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET | `/login?next=<경로>` | 로그인 수단 선택. 로그인 후 `next`(같은 사이트 경로만, 기본 `/account`)로 이동 |
| GET | `/auth/{oauth}/start` | OAuth 로그인 화면으로 이동. `?link=1`이면 로그인한 계정에 연결 |
| GET | `/auth/{oauth}/callback` | OAuth가 돌아오는 주소. 계정을 찾거나 만들고 웹 세션 쿠키(`ebms_session`, 30일, 쓸 때마다 연장)를 발급 |
| POST | `/auth/logout` | 현재 웹 세션 종료 |
| GET | `/account` | 내 계정: 연결된 로그인 수단(연결·해제), 로그인된 기기(로그아웃) |
| DELETE | `/api/account/identities/{id}` | 로그인 수단 연결 해제. 마지막 1개면 `409` |
| DELETE | `/api/account/sessions/{id}` | 해당 기기 로그아웃. 없으면 `404` |

- 처음 보는 OAuth 계정으로 로그인하면 새 계정을 만듭니다. 이메일이 같아도 자동으로 합치지 않으니, 다른 OAuth는 로그인한 상태에서 `/account`의 "연결"로 추가하세요.
- 이미 다른 계정에 연결된 OAuth 계정은 연결할 수 없습니다(`409`).
- `/api/account/*`는 웹 세션 쿠키가 없거나 만료되면 `401`입니다.

## HTTP API

모든 오류 응답은 FastAPI 기본 형식인 JSON `{"detail": "<메시지>"}` 입니다. 아래 다운로드 API는 아직 인증 없이 열려 있습니다(로그인 필수는 이후 단계에서 적용).

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET | `/` | 웹 메인 페이지 (HTML) |
| GET | `/static/{path}` | 정적 파일 (CSS/JS/아이콘) |
| GET | `/api/version` | API 버전 |
| GET | `/api/charthash` | 차트 청크별 SHA-256 목록 |
| GET | `/api/files/chart/{chunk_id}` | 차트 청크 zip 다운로드 |
| GET | `/api/manifest/hash` | 매니페스트 청크별 SHA-256 목록 |
| GET | `/api/manifest/{chunk_id}` | 곡 매니페스트 (JSON) |
| GET | `/api/files/song/{chart_sha256}` | 차트가 속한 곡 zip 다운로드 |
| GET | `/api/files/song/id/{song_id}` | song id로 곡 zip 다운로드 |

### 파일 다운로드 공통 헤더

`/api/files/...` 응답은 모두 아래를 지원합니다.

- `X-Content-SHA256`, `ETag: "<sha256>"`: 파일 전체의 SHA-256 (부분 응답에도 전체 기준 값)
- `Range: bytes=a-b` / `bytes=a-` / `bytes=-n` (단일 범위): `206 Partial Content` + `Content-Range`. 범위 밖이면 `416` + `Content-Range: bytes */<크기>`. 여러 범위는 무시하고 `200`으로 전체를 보냅니다.
- `If-Range: "<sha256>"`: 값이 다르면 `Range`를 무시하고 전체를 보냅니다 (이어받기 중 파일이 바뀐 경우).
- `If-None-Match: "<sha256>"`: 같으면 `304`.

### `GET /api/version`

```json
{"api": 1, "server": "0.1.0"}
```

`api`는 호환되지 않는 변경이 있을 때 올라갑니다. `server`는 알 수 없으면 `null`입니다.

### `GET /api/charthash`

차트 청크 번호와 해당 청크 zip의 SHA-256 맵을 반환합니다. 클라이언트는 이 값을 로컬 청크와 비교해 바뀐 청크만 다시 받으면 됩니다.

- 파라미터: 없음
- 응답 `200` (`application/json`): 키는 청크 번호(JSON이라 문자열), 값은 SHA-256 hex. 청크가 없으면 `{}`.

```json
{"0": "3f1c...e9", "1": "a7b2...04"}
```

### `GET /api/files/chart/{chunk_id}`

차트 파일(`.bms`, `.bme`, `.bml`, `.pms`)을 묶은 청크 zip(무압축)을 내려줍니다. 청크는 64MB를 넘으면 다음 번호로 넘어가며, 마지막 청크는 새 곡이 추가될 때마다 내용과 해시가 바뀝니다.

zip 안 항목 이름은 `{차트 sha256}{확장자 소문자}` (예: `3f1c...e9.bms`)입니다. 원래 파일명과 소속 곡은 매니페스트에서 확인합니다.

- 경로 파라미터: `chunk_id` (int, 0부터 시작)
- 응답 `200` (`application/zip`): `chart_chunk_{chunk_id:05d}.zip` (예: `chart_chunk_00000.zip`)
- 오류
  - `404` `File not found`: 없는 청크 번호
  - `422`: `chunk_id`가 정수가 아님

### `GET /api/manifest/hash`

매니페스트 청크 번호와 SHA-256 맵. 형식은 `/api/charthash`와 같습니다. 해시는 **압축을 푼 JSON** 기준입니다.

### `GET /api/manifest/{chunk_id}`

곡 목록을 다운로드 없이 확인하기 위한 매니페스트입니다. 청크 `n`에는 `song_id`가 `n*1000` 이상 `(n+1)*1000` 미만인 곡이 들어갑니다. 곡이 추가되거나 기존 곡에 차트가 연결되면 해당 청크가 갱신됩니다.

- 응답 `200` (`application/json`): `Accept-Encoding`에 `gzip`이 있으면 `Content-Encoding: gzip`으로 보냅니다.
- 오류: `404` `Manifest not found`

```json
[
  {
    "song_id": 123,
    "folder": "Artist - Title",
    "zip_size": 51234567,
    "zip_sha256": "...",
    "charts": ["<차트 sha256>", "..."],
    "chart_files": [
      {"sha256": "<차트 sha256>", "path": "_7a.bme", "size": 12345}
    ],
    "files": [
      {"path": "bgm01.ogg", "size": 12345, "offset": 0, "comp_size": 12000, "crc32": "1a2b3c4d", "method": 8}
    ]
  }
]
```

- `folder`: 등록할 때의 곡 폴더명. 이전 버전에서 등록된 곡은 song id 문자열입니다.
- `chart_files`: 차트별 SHA-256, 파일 경로(곡 zip 안 경로 또는 등록할 때의 파일명), 파일 크기 목록.
- `files`: 곡 zip 안의 파일(디렉터리 제외). `offset`은 local file header 위치, `method`는 zip 압축 방식(0 무압축, 8 Deflate)입니다. 파일 하나만 받으려면 `offset`부터 30바이트를 받아 파일명/extra 길이(26~29바이트)를 읽고, 그 뒤 `comp_size` 바이트를 `Range`로 받습니다.

### `GET /api/files/song/id/{song_id}`

song id로 곡 zip을 내려줍니다. 응답은 `/api/files/song/{chart_sha256}`과 같습니다.

- 오류: `404` `song not found`, `422` `song_id`가 정수가 아님

### `GET /api/files/song/{chart_sha256}`

차트 파일의 SHA-256으로 그 차트가 속한 곡 폴더 전체(키음, BGA 등 포함) zip을 내려줍니다.

- 경로 파라미터: `chart_sha256` (string, 차트 파일 내용의 SHA-256 hex 소문자 64자)
- 응답 `200` (`application/zip`): 곡 zip (Deflate 압축, 폴더 구조 유지)
- 오류
  - `404` `chart file not found`: 등록되지 않은 차트 해시
  - `404` `chart file found but song file not found. report this to admin.`: 차트는 있으나 곡 데이터가 없음 (서버 데이터 불일치)

### 곡 등록

> `var/tmp/` 임포트는 테스트용 임시 업로드 경로입니다. 추후 관리자 페이지를 통한 업로드 방식이 추가될 예정입니다.

곡 등록용 HTTP API는 아직 없습니다. 현재는 서버 시작 시 `var/tmp/` 아래 폴더를 훑어 차트 파일이 있는 폴더를 곡으로 등록하고, 등록한 폴더는 삭제합니다. 같은 차트(SHA-256과 크기 동일)가 이미 있으면 기존 곡에 연결됩니다.

### 이전 버전 데이터 마이그레이션

서버가 시작할 때 없는 컬럼을 추가하고 기존 곡의 매니페스트를 채웁니다. 차트 청크의 항목 이름 변경은 한 번 직접 실행합니다 (청크 해시가 바뀌어 클라이언트가 다시 받습니다).

```bash
docker compose exec ebms python -m ebms_server.migrate
```

