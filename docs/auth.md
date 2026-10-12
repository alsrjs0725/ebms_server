# 로그인 설정 (Google, Discord)

계정은 내부 UUID로 관리하고, 한 계정에 Google·Discord를 함께 연결할 수 있습니다. `.env`에 아래 값을 넣고 `docker compose up -d`로 다시 올리면 됩니다. 키를 넣지 않은 OAuth는 로그인 페이지에 나오지 않습니다.

| 변수 | 설명 |
| --- | --- |
| `EBMS_PUBLIC_URL` | 브라우저가 접속하는 서버 주소 (예: `https://ebms.example.com`). `https://`면 쿠키에 `Secure`가 붙습니다 |
| `EBMS_SECRET_KEY` | 쿠키 서명용 임의 문자열. `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `EBMS_GOOGLE_CLIENT_ID`, `EBMS_GOOGLE_CLIENT_SECRET` | Google OAuth 클라이언트 |
| `EBMS_DISCORD_CLIENT_ID`, `EBMS_DISCORD_CLIENT_SECRET` | Discord OAuth 앱 |
| `EBMS_ADMIN_USER_IDS` | 관리자로 지정할 계정 ID(UUID, `/account`의 "계정 ID"). 쉼표로 구분. 로그인하거나 세션을 쓸 때 승격하며, 승격만 하므로 목록에서 빼도 강등되지 않음(관리자 페이지에서 강등) |

처음 관리자를 정할 때는 웹에 한 번 로그인해 `/account`에서 계정 ID를 확인하고, `.env`에 넣은 뒤 서버를 다시 올립니다. 다시 로그인하지 않아도 다음 요청부터 관리자입니다.

## 수집하는 정보

OAuth 계정 ID(Google `sub`, Discord `id`)만 저장합니다. 이메일·계정 이름은 요청하거나 저장하지 않고, 화면에 보이는 이름은 처음 로그인할 때 사용자가 직접 정하는 닉네임입니다(`/signup`, 이후 `/account/nickname`에서 변경). 예전 DB를 올리면 OAuth에서 가져온 이름을 비우므로 기존 사용자는 다음 웹 로그인 때 닉네임을 정합니다. 세션에도 기기 이름(브라우저 User-Agent, PC 호스트명)을 저장하지 않습니다. 로그인된 기기 목록은 종류(웹/클라이언트)와 로그인·마지막 사용 시각으로 구분합니다(#102).

## Google

1. [Google Cloud Console](https://console.cloud.google.com/apis/credentials) → 사용자 인증 정보 만들기 → **OAuth 클라이언트 ID** → 애플리케이션 유형 **웹 애플리케이션**
2. 승인된 리디렉션 URI: `<EBMS_PUBLIC_URL>/auth/google/callback`
3. OAuth 동의 화면의 범위는 `openid`만 씁니다(이메일·프로필은 요청하지 않음).
4. 발급된 클라이언트 ID/보안 비밀을 `EBMS_GOOGLE_CLIENT_ID`/`EBMS_GOOGLE_CLIENT_SECRET`에 넣습니다.

## Discord

1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application → **OAuth2**
2. Redirects에 `<EBMS_PUBLIC_URL>/auth/discord/callback` 추가
3. Client ID/Client Secret을 `EBMS_DISCORD_CLIENT_ID`/`EBMS_DISCORD_CLIENT_SECRET`에 넣습니다. 요청 범위는 `identify`입니다(계정 ID만 저장, 이메일은 요청하지 않음).

## 웹 페이지

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET | `/` | 홈. 로그인 상태에 따라 로그인/가입 또는 설정·관리자 홈 링크 |
| GET | `/login?next=<경로>` | 로그인 수단 선택. 로그인 후 `next`(같은 사이트 경로만, 기본 `/account`)로 이동 |
| GET | `/auth/{oauth}/start` | OAuth 로그인 화면으로 이동. `?link=1`이면 로그인한 계정에 연결 |
| GET | `/auth/{oauth}/callback` | OAuth가 돌아오는 주소. 계정을 찾으면 웹 세션 쿠키(`ebms_session`, 30일, 쓸 때마다 연장)를 발급, 처음 보는 계정이면 `/signup`으로 |
| GET/POST | `/signup` | 처음 보는 OAuth 계정의 가입. 닉네임(1~32자)을 받아 계정을 만들고 로그인 |
| GET/POST | `/account/nickname` | 닉네임 변경. 닉네임이 없는 계정은 로그인 직후 이 화면으로 |
| POST | `/auth/logout` | 현재 웹 세션 종료 |
| GET | `/account` | 설정(내 계정): 연결된 로그인 수단(연결·해제), 로그인된 기기(로그아웃), 계정 삭제 |
| DELETE | `/api/account/identities/{id}` | 로그인 수단 연결 해제. 마지막 1개면 `409` |
| DELETE | `/api/account/sessions/{id}` | 해당 기기 로그아웃. 없으면 `404` |
| DELETE | `/api/account` | 회원 탈퇴. 계정과 로그인 수단·세션(클라이언트 포함)·티켓·다운로드 기록을 지우고 쿠키를 지움. 마지막 관리자면 `409` |

- 처음 보는 OAuth 계정으로 로그인하면 닉네임을 정해 새 계정을 만듭니다. 다른 OAuth 계정을 자동으로 합치지 않으니, 다른 OAuth는 로그인한 상태에서 `/account`의 "연결"로 추가하세요.
- 이미 다른 계정에 연결된 OAuth 계정은 연결할 수 없습니다(`409`).
- `/api/account`와 `/api/account/*`는 웹 세션(쿠키)으로만 쓸 수 있습니다. 세션이 없거나 만료되면 `401`, 클라이언트 세션키(Bearer)면 `403`입니다. 클라이언트는 자기 세션 로그아웃(`/api/auth/client/logout`)만 합니다.

## 클라이언트 로그인

클라이언트는 브라우저 로그인 결과를 루프백 리다이렉트 + PKCE(RFC 8252)로 받아 세션키로 바꿉니다. 세션키는 90일 유효하고 쓸 때마다 연장되며, `/account`의 로그인된 기기에 "클라이언트"로 나옵니다.

1. 클라이언트가 `127.0.0.1`의 빈 포트에서 수신을 열고 `state`, `code_verifier`(43~128자)를 만든 뒤 브라우저로 `/auth/client/authorize`를 엽니다.
2. 웹에 로그인돼 있지 않으면 로그인 페이지를 거칩니다. 서버는 1분짜리 1회용 `code`를 붙여 `<redirect_uri>?code=…&state=…`로 보냅니다.
3. 클라이언트는 `state`를 확인하고 `code`와 `code_verifier`를 `/api/auth/client/token`에 보내 세션키를 받습니다.
4. 이후 요청에는 `Authorization: Bearer <세션키>` 헤더를 붙입니다.

| 메서드 | 경로 | 인증 | 설명 |
| --- | --- | --- | --- |
| GET | `/auth/client/authorize` | 웹 쿠키(없으면 `/login`으로) | 쿼리: `redirect_uri`(`http://127.0.0.1:<포트>/…`만 허용), `state`, `code_challenge`(base64url SHA-256), `code_challenge_method=S256`. 예전 클라이언트가 보내는 `device_name`은 무시합니다. 값이 틀리면 `400` 페이지 |
| POST | `/api/auth/client/token` | - | JSON `{"code", "code_verifier"}` → `{"session_key", "expires_at", "user": {"id", "display_name", "role"}}`. 코드가 없거나 만료·사용됐거나 verifier가 틀리면 `400`(코드는 한 번 시도하면 폐기), 정지된 계정은 `403` |
| POST | `/api/auth/client/logout` | Bearer | 현재 세션키 폐기. `204` |
| GET | `/api/me` | Bearer 또는 웹 쿠키 | 내 계정: `id`, `display_name`, `role`, `oauths`(`[{"oauth", "name"}]`, `name`은 예전 클라이언트 호환용으로 항상 빈 값), `session.kind`(`client`/`web`), `tickets`, `pre`([다운로드](download.md) 참고) |

- `Authorization` 헤더가 있으면 쿠키는 보지 않습니다. 세션키가 틀리거나 만료·폐기됐으면 `401` + `WWW-Authenticate: Bearer`입니다.
- 웹 쿠키 값은 세션키로 쓸 수 없고, 그 반대도 마찬가지입니다.
