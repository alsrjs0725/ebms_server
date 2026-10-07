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
- `/api/account/*`는 세션(웹 쿠키 또는 클라이언트 세션키)이 없거나 만료되면 `401`입니다.

### 클라이언트 로그인

클라이언트는 브라우저 로그인 결과를 루프백 리다이렉트 + PKCE(RFC 8252)로 받아 세션키로 바꿉니다. 세션키는 90일 유효하고 쓸 때마다 연장되며, `/account`의 로그인된 기기에 "클라이언트"로 나옵니다.

1. 클라이언트가 `127.0.0.1`의 빈 포트에서 수신을 열고 `state`, `code_verifier`(43~128자)를 만든 뒤 브라우저로 `/auth/client/authorize`를 엽니다.
2. 웹에 로그인돼 있지 않으면 로그인 페이지를 거칩니다. 서버는 1분짜리 1회용 `code`를 붙여 `<redirect_uri>?code=…&state=…`로 보냅니다.
3. 클라이언트는 `state`를 확인하고 `code`와 `code_verifier`를 `/api/auth/client/token`에 보내 세션키를 받습니다.
4. 이후 요청에는 `Authorization: Bearer <세션키>` 헤더를 붙입니다.

| 메서드 | 경로 | 인증 | 설명 |
| --- | --- | --- | --- |
| GET | `/auth/client/authorize` | 웹 쿠키(없으면 `/login`으로) | 쿼리: `redirect_uri`(`http://127.0.0.1:<포트>/…`만 허용), `state`, `code_challenge`(base64url SHA-256), `code_challenge_method=S256`, `device_name`(선택, 기기 목록에 표시). 값이 틀리면 `400` 페이지 |
| POST | `/api/auth/client/token` | - | JSON `{"code", "code_verifier"}` → `{"session_key", "expires_at", "user": {"id", "display_name", "email", "role"}}`. 코드가 없거나 만료·사용됐거나 verifier가 틀리면 `400`(코드는 한 번 시도하면 폐기), 정지된 계정은 `403` |
| POST | `/api/auth/client/logout` | Bearer | 현재 세션키 폐기. `204` |
| GET | `/api/me` | Bearer 또는 웹 쿠키 | 내 계정: `id`, `display_name`, `email`, `role`, `oauths`(`[{"oauth", "name"}]`), `session.kind`(`client`/`web`), `tickets`, `pre`(아래 다운로드 절) |

- `Authorization` 헤더가 있으면 쿠키는 보지 않습니다. 세션키가 틀리거나 만료·폐기됐으면 `401` + `WWW-Authenticate: Bearer`입니다.
- 웹 쿠키 값은 세션키로 쓸 수 없고, 그 반대도 마찬가지입니다.

## 다운로드 API (사전 / 플레이)

다운로드는 두 갈래이고 둘 다 로그인(Bearer 세션키 또는 웹 쿠키)이 필요합니다. 없거나 만료된 세션은 `401`입니다. 서버는 경로만 보고 어느 할당량에서 뺄지 정합니다.

| 구분 | 메서드·경로 | 설명 |
| --- | --- | --- |
| 사전 | GET `/api/pre/charthash` | 차트 청크별 SHA-256 |
| 사전 | GET `/api/pre/chart/{chunk_id}` | 차트 청크 zip (Range 지원) |
| 사전 | GET `/api/pre/manifest/hash`, `/api/pre/manifest/{chunk_id}` | 매니페스트. `files`의 각 항목에 `kind`(`pre`/`play`) |
| 사전 | GET `/api/pre/song/{song_id}/file?path=<zip 안 경로>` | 곡의 사전 파일 하나를 압축을 풀어 보냅니다. `ETag`/`If-None-Match` 지원. `kind`가 `pre`가 아니면 `403` `not a pre-download file`, 없으면 `404` |
| 플레이 | GET `/api/play/song/{song_id}` | 곡 zip 전체(Range 이어받기 지원). 본문을 보낼 때 티켓 1개 |

**사전/플레이 판정**: 곡을 등록할 때(기존 곡은 서버 시작 시) 정합니다. 차트 파일, 차트 헤더 `#BANNER`·`#STAGEFILE`·`#BACKBMP`·`#PREVIEW`가 가리키는 파일, 이름이 `preview`로 시작하는 파일은 `pre`, 나머지(키음, BGA 등)는 `play`입니다. 헤더 경로는 대소문자·`\`를 무시하고, 확장자가 달라도 같은 종류(이미지끼리, 오디오끼리)면 같은 파일로 봅니다.

**플레이 티켓**: 곡 1개당 1개를 쓰고, 차감 후 `grant_seconds`(기본 30분) 동안 같은 곡은 다시 받아도 차감하지 않습니다. 티켓은 `refill_seconds`(기본 60초)마다 1개씩 `max_tickets`(기본 5개)까지 찹니다. 티켓이 없으면 `429` + `Retry-After: <다음 티켓까지 초>` + `{"detail": "no download ticket"}`입니다. `304`·`416`·`404`는 차감하지 않습니다.

**사전 다운로드 사용량**: 실제 보낸 바이트(매니페스트는 gzip이면 압축된 크기)를 월별로 더합니다. 월 경계는 KST 1일 0시입니다. 이번 달 사용량이 `pre_monthly_bytes`(기본 10GB)를 넘으면 끊지 않고 `pre_throttled_kbps`(기본 500Kbps)로 감속합니다. 같은 사용자의 동시 요청은 이 속도를 나눠 씁니다(프로세스 메모리, 워커 1개 전제). 해시 목록(`/hash`, `/charthash`)은 세지 않습니다.

`/api/me`에 남은 양이 나옵니다.

```json
{
  "tickets": {"available": 3, "max": 5, "refill_seconds": 60, "next_refill_at": 1791315327},
  "pre": {"month": "2026-10", "used_bytes": 123456, "limit_bytes": 10737418240, "throttled_kbps": 500, "throttled": false}
}
```

`next_refill_at`은 다음 티켓 1개가 차는 시각(unix 초)이고, 가득 차 있으면 `null`입니다.

## 관리자 페이지

`EBMS_ADMIN_EMAILS`의 계정으로 웹에 로그인하면 `/admin`을 쓸 수 있습니다(`/account`에 링크). 관리자 API는 웹 세션 쿠키로만 쓸 수 있고, 로그인하지 않았으면 `401`, 관리자가 아니면 `403`입니다.

- 전역 기본값 일괄 수정: 최대 티켓, 리필 시간, 같은 곡 재차감 없음 시간, 사전 다운로드 월 한도, 초과 후 속도. 처음 시작할 때 기본값을 넣고, 이후 바꾼 값은 재시작해도 유지됩니다.
- 사용자 검색(이름·이메일·계정 ID), 사용자별 한도 덮어쓰기(비우면 전역 기본값), 티켓 즉시 충전, 정지·해제.

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET·PUT | `/api/admin/settings` | 전역 기본값. PUT은 보낸 항목만 바꾸고, 하나라도 틀리면 아무것도 바꾸지 않고 `400`. 항목: `max_tickets`, `refill_seconds`, `grant_seconds`, `pre_monthly_bytes`(바이트), `pre_throttled_kbps` |
| GET | `/api/admin/users?q=` | 사용자 목록(최근 로그인 순 50명). 각 항목에 `overrides`, `tickets`, `pre` |
| GET·PUT | `/api/admin/users/{id}` | 사용자 상세·수정. PUT 항목: `status`(`active`/`banned`), `max_tickets`, `refill_seconds`, `pre_monthly_bytes`, `pre_throttled_kbps`(null이면 전역 기본값). 자기 자신은 정지할 수 없음 |
| POST | `/api/admin/users/{id}/refill` | 티켓을 최대치로 채움 |

## HTTP API

모든 오류 응답은 FastAPI 기본 형식인 JSON `{"detail": "<메시지>"}` 입니다. 로그인하지 않으면 `/api/version`(과 세션키를 받는 `/api/auth/client/token`) 외의 모든 API가 `401`입니다. 곡 목록(차트 해시, 매니페스트)도 마찬가지입니다.

API 버전 2에서 기존 공개 다운로드 API(`/api/charthash`, `/api/manifest/*`, `/api/files/*`)를 없앴습니다. 위의 사전/플레이 API를 쓰세요.

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET | `/` | 웹 메인 페이지 (HTML) |
| GET | `/static/{path}` | 정적 파일 (CSS/JS/아이콘) |
| GET | `/api/version` | API 버전. `auth`에 로그인 가능한 OAuth 목록(예: `["google", "discord"]`) |
| GET | `/api/pre/charthash` | 차트 청크별 SHA-256 목록 |
| GET | `/api/pre/chart/{chunk_id}` | 차트 청크 zip 다운로드 |
| GET | `/api/pre/manifest/hash` | 매니페스트 청크별 SHA-256 목록 |
| GET | `/api/pre/manifest/{chunk_id}` | 곡 매니페스트 (JSON) |
| GET | `/api/pre/song/{song_id}/file?path=` | 곡의 사전 파일 하나 |
| GET | `/api/play/song/{song_id}` | 곡 zip 다운로드 (티켓) |

### 파일 다운로드 공통 헤더

`/api/pre/chart/...`, `/api/play/song/...` 응답은 모두 아래를 지원합니다.

- `X-Content-SHA256`, `ETag: "<sha256>"`: 파일 전체의 SHA-256 (부분 응답에도 전체 기준 값)
- `Range: bytes=a-b` / `bytes=a-` / `bytes=-n` (단일 범위): `206 Partial Content` + `Content-Range`. 범위 밖이면 `416` + `Content-Range: bytes */<크기>`. 여러 범위는 무시하고 `200`으로 전체를 보냅니다.
- `If-Range: "<sha256>"`: 값이 다르면 `Range`를 무시하고 전체를 보냅니다 (이어받기 중 파일이 바뀐 경우).
- `If-None-Match: "<sha256>"`: 같으면 `304`.

### `GET /api/version`

```json
{"api": 2, "server": "0.1.0", "auth": ["google", "discord"]}
```

`api`는 호환되지 않는 변경이 있을 때 올라갑니다. `server`는 알 수 없으면 `null`입니다. 로그인 없이 쓸 수 있는 유일한 API라, 클라이언트는 서버를 추가할 때 이것으로 호환 여부를 확인합니다.

### `GET /api/pre/charthash`

차트 청크 번호와 해당 청크 zip의 SHA-256 맵을 반환합니다. 클라이언트는 이 값을 로컬 청크와 비교해 바뀐 청크만 다시 받으면 됩니다.

- 파라미터: 없음
- 응답 `200` (`application/json`): 키는 청크 번호(JSON이라 문자열), 값은 SHA-256 hex. 청크가 없으면 `{}`.

```json
{"0": "3f1c...e9", "1": "a7b2...04"}
```

### `GET /api/pre/chart/{chunk_id}`

차트 파일(`.bms`, `.bme`, `.bml`, `.pms`)을 묶은 청크 zip(무압축)을 내려줍니다. 청크는 64MB를 넘으면 다음 번호로 넘어가며, 마지막 청크는 새 곡이 추가될 때마다 내용과 해시가 바뀝니다.

zip 안 항목 이름은 `{차트 sha256}{확장자 소문자}` (예: `3f1c...e9.bms`)입니다. 원래 파일명과 소속 곡은 매니페스트에서 확인합니다.

- 경로 파라미터: `chunk_id` (int, 0부터 시작)
- 응답 `200` (`application/zip`): `chart_chunk_{chunk_id:05d}.zip` (예: `chart_chunk_00000.zip`)
- 오류
  - `404` `File not found`: 없는 청크 번호
  - `422`: `chunk_id`가 정수가 아님

### `GET /api/pre/manifest/hash`

매니페스트 청크 번호와 SHA-256 맵. 형식은 `/api/pre/charthash`와 같습니다. 해시는 **압축을 푼 JSON** 기준입니다.

### `GET /api/pre/manifest/{chunk_id}`

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
      {"path": "bgm01.ogg", "size": 12345, "offset": 0, "comp_size": 12000, "crc32": "1a2b3c4d", "method": 8, "kind": "play"}
    ]
  }
]
```

- `folder`: 등록할 때의 곡 폴더명. 이전 버전에서 등록된 곡은 song id 문자열입니다.
- `chart_files`: 차트별 SHA-256, 파일 경로(곡 zip 안 경로 또는 등록할 때의 파일명), 파일 크기 목록.
- `files`: 곡 zip 안의 파일(디렉터리 제외). `offset`은 local file header 위치, `method`는 zip 압축 방식(0 무압축, 8 Deflate), `kind`는 사전(`pre`)/플레이(`play`) 구분입니다. 파일 하나만 받으려면 `offset`부터 30바이트를 받아 파일명/extra 길이(26~29바이트)를 읽고, 그 뒤 `comp_size` 바이트를 `Range`로 받습니다.

### `GET /api/play/song/{song_id}`

곡 폴더 전체(키음, BGA 등 포함) zip을 내려줍니다. song id는 매니페스트에서 얻습니다. 티켓 규칙은 위 "플레이 티켓"을 보세요.

- 응답 `200`/`206` (`application/zip`): 곡 zip (Deflate 압축, 폴더 구조 유지)
- 오류: `404` `song not found`, `422` `song_id`가 정수가 아님, `429` `no download ticket`

### 곡 등록

> `var/tmp/` 임포트는 테스트용 임시 업로드 경로입니다. 추후 관리자 페이지를 통한 업로드 방식이 추가될 예정입니다.

곡 등록용 HTTP API는 아직 없습니다. 현재는 서버 시작 시 `var/tmp/` 아래 폴더를 훑어 차트 파일이 있는 폴더를 곡으로 등록하고, 등록한 폴더는 삭제합니다. 같은 차트(SHA-256과 크기 동일)가 이미 있으면 기존 곡에 연결됩니다.

### 이전 버전 데이터 마이그레이션

서버가 시작할 때 없는 컬럼을 추가하고 기존 곡의 매니페스트를 채웁니다. 차트 청크의 항목 이름 변경은 한 번 직접 실행합니다 (청크 해시가 바뀌어 클라이언트가 다시 받습니다).

```bash
docker compose exec ebms python -m ebms_server.migrate
```

