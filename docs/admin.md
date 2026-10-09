# 관리자 페이지

`EBMS_ADMIN_EMAILS`의 계정으로 웹에 로그인하거나 관리자 페이지에서 admin으로 지정된 사용자는 `/admin`을 쓸 수 있습니다(`/account`에 링크). `EBMS_ADMIN_EMAILS`는 로그인할 때 승격만 하고 강등은 하지 않으므로, 관리자를 내리려면 관리자 페이지에서 role을 바꿉니다. 관리자 API는 웹 세션 쿠키로만 쓸 수 있고, 로그인하지 않았으면 `401`, 관리자가 아니면 `403`입니다.

- 전역 기본값 일괄 수정: 최대 티켓, 리필 시간, 같은 곡 재차감 없음 시간, 사전 다운로드 월 한도, 초과 후 속도. 처음 시작할 때 기본값을 넣고, 이후 바꾼 값은 재시작해도 유지됩니다.
- 사용자 검색(이름·이메일·계정 ID), 사용자별 한도 덮어쓰기(비우면 전역 기본값), 티켓 즉시 충전, 정지·해제.
- 공지(`/admin/notices`): 클라이언트가 켜질 때 띄울 공지를 쓰고 고치고 지웁니다. 게시 여부와 시작·종료 시각을 정할 수 있고, 클라이언트는 사용자가 확인한 공지를 다시 띄우지 않습니다(고치면 다시 띄움).
- 곡 임포트(`/admin/import`): zip을 올려 곡을 등록하거나, 서버의 `var/tmp/`를 지금 가져옵니다. 차트 파일이 있는 폴더가 곡 하나이고, zip 최상위에 차트가 있으면 zip 이름을 곡 폴더명으로 씁니다. UTF-8 플래그가 없는 zip의 파일 이름은 UTF-8, cp932, cp949 순서로 읽습니다. 곡 폴더 안의 심볼릭 링크(파일·폴더)와 일반 파일이 아닌 항목은 따라가지 않고 건너뛰며 경고 로그를 남깁니다.
  - 브라우저가 zip을 조각(처음 32MB, 프록시가 `413`을 주거나 연결을 끊으면 절반씩 줄여 최소 256KB)으로 나눠 `var/import/`에 이어 붙이므로, 앞단 프록시의 요청 크기 제한과 상관없이 수십 GB도 올릴 수 있습니다. 끊기면 받은 곳부터 다시 보냅니다.
  - 다 받으면 서버가 백그라운드에서 곡을 하나씩 풀어 등록하고 zip을 지웁니다. 디스크는 zip 크기 + 2GB가 필요합니다(모자라면 시작할 때 `507`). 진행 상황은 페이지의 **등록 작업**에 보이며, 다 올린 뒤에는 페이지를 닫아도 됩니다. 작업 목록은 메모리에만 있어 재시작하면 비워지고, 재시작 시 `var/import/`의 남은 파일은 지웁니다.
  - 곡은 여러 개를 한 트랜잭션(배치)으로 넣고, 차트·매니페스트·사전 청크는 배치를 커밋할 때 한 번씩만 갱신합니다. 새 차트 64MB, 곡 데이터 1GB, 500곡 중 먼저 차는 쪽에서 커밋합니다(`IMPORT_BATCH_*`). 도중에 꺼지면 그 배치는 통째로 롤백되고 다시 임포트하면 들어갑니다. 곡 하나가 실패하면 그 곡만 되돌립니다.

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET·PUT | `/api/admin/settings` | 전역 기본값. PUT은 보낸 항목만 바꾸고, 하나라도 틀리면 아무것도 바꾸지 않고 `400`. 항목: `max_tickets`, `refill_seconds`, `grant_seconds`, `pre_monthly_bytes`(바이트), `pre_throttled_kbps` |
| GET | `/api/admin/users?q=` | 사용자 목록(최근 로그인 순 50명). 각 항목에 `overrides`, `tickets`, `pre` |
| GET·PUT | `/api/admin/users/{id}` | 사용자 상세·수정. PUT 항목: `status`(`active`/`banned`), `max_tickets`, `refill_seconds`, `pre_monthly_bytes`, `pre_throttled_kbps`(null이면 전역 기본값). 자기 자신은 정지할 수 없음 |
| POST | `/api/admin/users/{id}/refill` | 티켓을 최대치로 채움 |
| POST | `/api/admin/import/uploads` | zip 조각 업로드 시작. 본문 `{filename, size}` → `{id, received, size}`. 디스크 부족이면 `507` |
| PUT | `/api/admin/import/uploads/{id}?offset=` | 본문(바이트)을 이어 씀 → `{received}`. `offset`이 받은 크기와 다르면 `409`(`detail`에 현재 위치), 선언한 크기를 넘으면 `400` |
| DELETE | `/api/admin/import/uploads/{id}` | 업로드 취소(받은 파일 삭제) |
| POST | `/api/admin/import/uploads/{id}/finish` | 다 받은 zip의 등록 작업을 시작 → 작업. 덜 받았으면 `409` |
| POST | `/api/admin/import/tmp` | `var/tmp/`의 곡 폴더와 zip을 등록하는 작업 시작(등록한 폴더와, 모든 곡을 등록한 zip은 지움). 이미 돌고 있으면 그 작업을 반환 → 작업 |
| GET·POST | `/api/admin/notices` | 공지 목록(최근 순)·새 공지. 본문 `{title, body, level(info/warning), published, starts_at, ends_at}`(시각은 unix 초, null이면 제한 없음). 틀리면 `422` |
| PUT·DELETE | `/api/admin/notices/{id}` | 공지 수정(본문은 POST와 같음, 전체를 바꿈)·삭제 |
| GET | `/api/admin/import/jobs`, `/api/admin/import/jobs/{id}` | 작업(최근 순). `{id, kind, name, status(running/done/failed), total, done, songs, error}`, 곡마다 `{folder, song_id, new_song, charts, new_charts}` 또는 `{folder, error}` |
