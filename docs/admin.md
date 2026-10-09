# 관리자 페이지

`EBMS_ADMIN_EMAILS`의 계정으로 웹에 로그인하면 `/admin`을 쓸 수 있습니다(`/account`에 링크). 관리자 API는 웹 세션 쿠키로만 쓸 수 있고, 로그인하지 않았으면 `401`, 관리자가 아니면 `403`입니다.

- 전역 기본값 일괄 수정: 최대 티켓, 리필 시간, 같은 곡 재차감 없음 시간, 사전 다운로드 월 한도, 초과 후 속도. 처음 시작할 때 기본값을 넣고, 이후 바꾼 값은 재시작해도 유지됩니다.
- 사용자 검색(이름·이메일·계정 ID), 사용자별 한도 덮어쓰기(비우면 전역 기본값), 티켓 즉시 충전, 정지·해제.
- 곡 임포트(`/admin/import`): zip을 올려 곡을 등록하거나, 서버의 `var/tmp/`를 지금 가져옵니다. 차트 파일이 있는 폴더가 곡 하나이고, zip 최상위에 차트가 있으면 zip 이름을 곡 폴더명으로 씁니다. UTF-8 플래그가 없는 zip의 파일 이름은 UTF-8, cp932, cp949 순서로 읽습니다. 올린 zip은 `var/import/`에 풀었다가 등록 후 지웁니다.

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET·PUT | `/api/admin/settings` | 전역 기본값. PUT은 보낸 항목만 바꾸고, 하나라도 틀리면 아무것도 바꾸지 않고 `400`. 항목: `max_tickets`, `refill_seconds`, `grant_seconds`, `pre_monthly_bytes`(바이트), `pre_throttled_kbps` |
| GET | `/api/admin/users?q=` | 사용자 목록(최근 로그인 순 50명). 각 항목에 `overrides`, `tickets`, `pre` |
| GET·PUT | `/api/admin/users/{id}` | 사용자 상세·수정. PUT 항목: `status`(`active`/`banned`), `max_tickets`, `refill_seconds`, `pre_monthly_bytes`, `pre_throttled_kbps`(null이면 전역 기본값). 자기 자신은 정지할 수 없음 |
| POST | `/api/admin/users/{id}/refill` | 티켓을 최대치로 채움 |
| POST | `/api/admin/import` | zip 업로드(multipart, 필드 `files` 여러 개). zip마다 `{file, error, songs}`, 곡마다 `{folder, song_id, new_song, charts, new_charts}` 또는 `{folder, error}` |
| POST | `/api/admin/import/tmp` | `var/tmp/` 아래 곡을 등록하고 등록한 곡 폴더는 지움. 결과는 곡 목록(위와 같은 형식) |
