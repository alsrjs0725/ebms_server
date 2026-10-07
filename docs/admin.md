# 관리자 페이지

`EBMS_ADMIN_EMAILS`의 계정으로 웹에 로그인하면 `/admin`을 쓸 수 있습니다(`/account`에 링크). 관리자 API는 웹 세션 쿠키로만 쓸 수 있고, 로그인하지 않았으면 `401`, 관리자가 아니면 `403`입니다.

- 전역 기본값 일괄 수정: 최대 티켓, 리필 시간, 같은 곡 재차감 없음 시간, 사전 다운로드 월 한도, 초과 후 속도. 처음 시작할 때 기본값을 넣고, 이후 바꾼 값은 재시작해도 유지됩니다.
- 사용자 검색(이름·이메일·계정 ID), 사용자별 한도 덮어쓰기(비우면 전역 기본값), 티켓 즉시 충전, 정지·해제.

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET·PUT | `/api/admin/settings` | 전역 기본값. PUT은 보낸 항목만 바꾸고, 하나라도 틀리면 아무것도 바꾸지 않고 `400`. 항목: `max_tickets`, `refill_seconds`, `grant_seconds`, `pre_monthly_bytes`(바이트), `pre_throttled_kbps` |
| GET | `/api/admin/users?q=` | 사용자 목록(최근 로그인 순 50명). 각 항목에 `overrides`, `tickets`, `pre` |
| GET·PUT | `/api/admin/users/{id}` | 사용자 상세·수정. PUT 항목: `status`(`active`/`banned`), `max_tickets`, `refill_seconds`, `pre_monthly_bytes`, `pre_throttled_kbps`(null이면 전역 기본값). 자기 자신은 정지할 수 없음 |
| POST | `/api/admin/users/{id}/refill` | 티켓을 최대치로 채움 |
