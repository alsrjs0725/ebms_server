# 로그인 설정 (Google, Discord)

계정은 내부 UUID로 관리하고, 한 계정에 Google·Discord를 함께 연결할 수 있습니다. `.env`에 아래 값을 넣고 `docker compose up -d`로 다시 올리면 됩니다. 키를 넣지 않은 OAuth는 로그인 페이지에 나오지 않습니다.

| 변수 | 설명 |
| --- | --- |
| `EBMS_PUBLIC_URL` | 브라우저가 접속하는 서버 주소 (예: `https://ebms.example.com`). `https://`면 쿠키에 `Secure`가 붙습니다 |
| `EBMS_SECRET_KEY` | 쿠키 서명용 임의 문자열. `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `EBMS_GOOGLE_CLIENT_ID`, `EBMS_GOOGLE_CLIENT_SECRET` | Google OAuth 클라이언트 |
| `EBMS_DISCORD_CLIENT_ID`, `EBMS_DISCORD_CLIENT_SECRET` | Discord OAuth 앱 |
| `EBMS_ADMIN_EMAILS` | 이 이메일(OAuth가 확인한 것만)로 로그인하면 관리자로 지정. 쉼표로 구분. 승격만 하므로 목록에서 빼도 강등되지 않음(관리자 페이지에서 강등) |

## Google

1. [Google Cloud Console](https://console.cloud.google.com/apis/credentials) → 사용자 인증 정보 만들기 → **OAuth 클라이언트 ID** → 애플리케이션 유형 **웹 애플리케이션**
2. 승인된 리디렉션 URI: `<EBMS_PUBLIC_URL>/auth/google/callback`
3. OAuth 동의 화면의 범위는 `openid`, `email`, `profile`이면 충분합니다.
4. 발급된 클라이언트 ID/보안 비밀을 `EBMS_GOOGLE_CLIENT_ID`/`EBMS_GOOGLE_CLIENT_SECRET`에 넣습니다.

## Discord

1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application → **OAuth2**
2. Redirects에 `<EBMS_PUBLIC_URL>/auth/discord/callback` 추가
3. Client ID/Client Secret을 `EBMS_DISCORD_CLIENT_ID`/`EBMS_DISCORD_CLIENT_SECRET`에 넣습니다. 요청 범위는 `identify email`입니다.

## 웹 페이지

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET | `/` | 홈. 로그인 상태에 따라 로그인/가입 또는 설정·관리자 홈 링크 |
| GET | `/login?next=<경로>` | 로그인 수단 선택. 로그인 후 `next`(같은 사이트 경로만, 기본 `/account`)로 이동 |
| GET | `/auth/{oauth}/start` | OAuth 로그인 화면으로 이동. `?link=1`이면 로그인한 계정에 연결 |
| GET | `/auth/{oauth}/callback` | OAuth가 돌아오는 주소. 계정을 찾거나 만들고 웹 세션 쿠키(`ebms_session`, 30일, 쓸 때마다 연장)를 발급 |
| POST | `/auth/logout` | 현재 웹 세션 종료 |
| GET | `/account` | 설정(내 계정): 연결된 로그인 수단(연결·해제), 로그인된 기기(로그아웃) |
| DELETE | `/api/account/identities/{id}` | 로그인 수단 연결 해제. 마지막 1개면 `409` |
| DELETE | `/api/account/sessions/{id}` | 해당 기기 로그아웃. 없으면 `404` |

- 처음 보는 OAuth 계정으로 로그인하면 새 계정을 만듭니다. 이메일이 같아도 자동으로 합치지 않으니, 다른 OAuth는 로그인한 상태에서 `/account`의 "연결"로 추가하세요.
- 이미 다른 계정에 연결된 OAuth 계정은 연결할 수 없습니다(`409`).
- `/api/account/*`는 세션(웹 쿠키 또는 클라이언트 세션키)이 없거나 만료되면 `401`입니다.

## 클라이언트 로그인

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
| GET | `/api/me` | Bearer 또는 웹 쿠키 | 내 계정: `id`, `display_name`, `email`, `role`, `oauths`(`[{"oauth", "name"}]`), `session.kind`(`client`/`web`), `tickets`, `pre`([다운로드](download.md) 참고) |

- `Authorization` 헤더가 있으면 쿠키는 보지 않습니다. 세션키가 틀리거나 만료·폐기됐으면 `401` + `WWW-Authenticate: Bearer`입니다.
- 웹 쿠키 값은 세션키로 쓸 수 없고, 그 반대도 마찬가지입니다.
