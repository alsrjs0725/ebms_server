# 관리자 페이지

`EBMS_ADMIN_USER_IDS`에 계정 ID(UUID)를 넣은 사용자나 관리자 페이지에서 admin으로 지정된 사용자는 `/admin`을 쓸 수 있습니다(`/account`에 링크). `EBMS_ADMIN_USER_IDS`는 승격만 하고 강등은 하지 않으므로, 관리자를 내리려면 관리자 페이지에서 role을 바꿉니다. 관리자 API는 웹 세션 쿠키로만 쓸 수 있고, 로그인하지 않았으면 `401`, 관리자가 아니면 `403`입니다.

- 전역 기본값 일괄 수정: 최대 티켓, 리필 시간, 같은 곡 재차감 없음 시간, 사전 다운로드 월 한도, 초과 후 속도. 처음 시작할 때 기본값을 넣고, 이후 바꾼 값은 재시작해도 유지됩니다.
- 사용자 검색(닉네임·계정 ID), 사용자별 한도 덮어쓰기(비우면 전역 기본값), 티켓 즉시 충전, 정지·해제.
  - 사용자 목록·상세를 보거나 바꾸면 관리자 접속기록을 남깁니다: `var/log/audit.log`(1년 이상 보관)와 서버 로그(로거 `ebms_server.audit`). 한 줄에 관리자 id, 접속 IP, 행위(`user.search`·`user.view`·`user.update`·`user.refill`), 대상 사용자 id(검색이면 결과 전체), 검색어·바꾼 값이 들어갑니다. 안전성 확보조치 기준에 따라 월 1회 이상 로그 화면에서 이 로거로 걸러 점검합니다.
- 공지(`/admin/notices`): 클라이언트가 켜질 때 띄울 공지를 쓰고 고치고 지웁니다. 게시 여부와 시작·종료 시각을 정할 수 있고, 클라이언트는 사용자가 확인한 공지를 다시 띄우지 않습니다(고치면 다시 띄움).
- 처리방침·약관(`/admin/policies`): 개인정보 처리방침(`/privacy`)과 이용약관(`/terms`)을 고칩니다. 저장한 적이 없으면 `src/ebms_server/policy_templates/`의 기본 템플릿을 보여주며, 템플릿의 `[[...]]` 칸(운영자 이름, 연락처, 보유기간 등)을 채워 저장하세요. 저장할 때마다 시행일과 함께 새 판(`policy` 테이블)으로 쌓여 공개 페이지에 변경 이력으로 보입니다. 시행일을 미래로 두면 그날까지 지금 판을 보여주면서 "변경 예정"을 알립니다. 두 페이지는 로그인(가입) 화면, 메인, `/account`, 모든 페이지 아래에 링크됩니다. 본문은 마크다운 일부(제목, 목록, 표, 굵게, http(s)·mailto 링크)만 지원합니다.
- 곡 관리(`/admin/songs`): 곡 번호·폴더명·차트 파일명으로 곡을 찾고(최근 등록 순, 한 쪽에 50곡), 곡을 누르면 곡 zip의 파일 구조(폴더별, 차트·사전/플레이 표시, 종류별로 보이기/숨기기. 두 종류에 속한 파일은 둘 다 끄면 숨김)를 보고 파일 하나나 zip 전체를 받습니다(티켓·사용량을 쓰지 않음). **사용 중지**(삭제)는 사유를 적고 곡 번호를 다시 입력해야 합니다. 곡 zip과 차트(곡 zip에 없는 차트는 차트 청크에서 꺼냄)를 `var/unused/{곡 번호}/`(`song.zip`, `charts/{sha256}{확장자}`)로 옮기고 DB의 곡 데이터를 지웁니다. 사용 중지한 곡은 **사용 중지** 탭(`deleted_song` 테이블)에 폴더명·크기·차트 수·사유·지운 사람·시각과 함께 남고, 번호는 결번으로 두어 다른 곡에 쓰지 않습니다(AUTO_INCREMENT가 되돌아가도 결번 뒤부터 씀). **복원**하면 같은 번호로 되돌리고 옮겨 둔 폴더를 지웁니다. 복원할 차트가 다른 곡에 붙어 있으면 `409`로 복원하지 않습니다. 사용 중지·복원하면 그 곡의 매니페스트·사전 청크를 다시 만들어 클라이언트는 다음 동기화 때 목록에서 빼거나 다시 받습니다. 사용 중지한 곡을 요청하면 `410` `song deleted: <사유>`입니다. 차트 청크에서는 뺄 수 없어 차트는 곡 연결만 끊어 남깁니다. 사용 중지한 곡과 차트(sha256+크기)가 하나라도 겹치는 곡은 다시 임포트하지 않고 건너뛰며(경고 로그, 작업 결과에 `skipped: "unused"`와 그 곡 번호), `var/tmp/`의 원본은 처리한 것으로 보고 지웁니다. 다시 쓰려면 복원합니다. 임포트 배치가 쓰기 잠금을 10초 넘게 잡고 있으면 `409`이므로 잠시 뒤 다시 시도합니다.
- 곡 임포트(곡 관리의 **곡 임포트** 탭, `/admin/import`로 바로 열림): zip을 올려 곡을 등록하거나, 서버의 `var/tmp/`를 지금 가져옵니다. 차트 파일이 있는 폴더가 곡 하나이고, zip 최상위에 차트가 있으면 zip 이름을 곡 폴더명으로 씁니다. UTF-8 플래그가 없는 zip의 파일 이름은 UTF-8, cp932, cp949 순서로 읽습니다. 곡 폴더 안의 심볼릭 링크(파일·폴더)와 일반 파일이 아닌 항목은 따라가지 않고 건너뛰며 경고 로그를 남깁니다.
  - 브라우저가 zip을 조각(처음 32MB, 프록시가 `413`을 주거나 연결을 끊으면 절반씩 줄여 최소 256KB)으로 나눠 `var/import/`에 이어 붙이므로, 앞단 프록시의 요청 크기 제한과 상관없이 수십 GB도 올릴 수 있습니다. 끊기면 받은 곳부터 다시 보냅니다.
  - 다 받으면 서버가 백그라운드에서 곡을 하나씩 풀어 등록하고 zip을 지웁니다. 디스크는 zip 크기 + 2GB가 필요합니다(모자라면 시작할 때 `507`). 진행 상황은 페이지의 **등록 작업**에 보이며, 다 올린 뒤에는 페이지를 닫아도 됩니다. 작업 목록은 메모리에만 있어 재시작하면 비워지고, 재시작 시 `var/import/`의 남은 파일은 지웁니다.
  - 곡은 여러 개를 한 트랜잭션(배치)으로 넣고, 차트·매니페스트·사전 청크는 배치를 커밋할 때 한 번씩만 갱신합니다. 새 차트 64MB, 곡 데이터 1GB, 500곡 중 먼저 차는 쪽에서 커밋합니다(`IMPORT_BATCH_*`). 도중에 꺼지면 그 배치는 통째로 롤백되고 다시 임포트하면 들어갑니다. 곡 하나가 실패하면 그 곡만 되돌립니다.

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET·PUT | `/api/admin/settings` | 전역 기본값. PUT은 보낸 항목만 바꾸고, 하나라도 틀리면 아무것도 바꾸지 않고 `400`. 항목: `max_tickets`, `refill_seconds`, `grant_seconds`, `pre_monthly_bytes`(바이트), `pre_throttled_kbps` |
| GET | `/api/admin/users?q=` | 사용자 목록(최근 로그인 순 50명). 각 항목에 `overrides`, `tickets`, `pre` |
| GET·PUT | `/api/admin/users/{id}` | 사용자 상세·수정. PUT 항목: `status`(`active`/`banned`), `banned_until`(정지가 끝나는 unix 초, `status`와 함께. 없으면 무기한, 정지가 끝나면 자동 해제), `max_tickets`, `refill_seconds`, `pre_monthly_bytes`, `pre_throttled_kbps`(null이면 전역 기본값). 자기 자신은 정지할 수 없음 |
| POST | `/api/admin/users/{id}/refill` | 티켓을 최대치로 채움 |
| GET | `/api/admin/songs?q=&page=` | 곡 목록(번호 내림차순, 쪽마다 50곡, `page`는 1부터) → `{items, total, page, pages}`. 각 항목 `{id, folder, size, charts}` |
| GET·DELETE | `/api/admin/songs/{id}` | 곡 상세 `{id, folder, size, sha256, charts, files}`(파일마다 `{path, size, comp_size, kind}`, 차트면 `chart`) · 사용 중지(본문 `{reason}`, 1000자까지, 생략 가능). 없으면 `404`, 임포트 중이라 잠금을 못 잡으면 `409` |
| POST | `/api/admin/songs/{id}/restore` | 사용 중지한 곡을 같은 번호로 복원 → 곡 상세. 사용 중지한 곡이 아니면 `404`, 차트가 다른 곡에 쓰이거나 잠금을 못 잡으면 `409` |
| GET | `/api/admin/songs/deleted?q=&page=` | 사용 중지한 곡(번호 내림차순, 쪽마다 50개, 번호·폴더명·사유로 검색) → `{items, total, page, pages}`. 각 항목 `{song_id, folder, size, sha256, charts, reason, deleted_by, deleted_by_name, deleted_at}` |
| GET | `/api/admin/songs/{id}/zip` | 곡 zip 전체(Range 지원) |
| GET | `/api/admin/songs/{id}/file?path=` | 곡 zip의 파일 하나(압축 해제) |
| POST | `/api/admin/import/uploads` | zip 조각 업로드 시작. 본문 `{filename, size}` → `{id, received, size}`. 디스크 부족이면 `507` |
| PUT | `/api/admin/import/uploads/{id}?offset=` | 본문(바이트)을 이어 씀 → `{received}`. `offset`이 받은 크기와 다르면 `409`(`detail`에 현재 위치), 선언한 크기를 넘으면 `400` |
| DELETE | `/api/admin/import/uploads/{id}` | 업로드 취소(받은 파일 삭제) |
| POST | `/api/admin/import/uploads/{id}/finish` | 다 받은 zip의 등록 작업을 시작 → 작업. 덜 받았으면 `409` |
| POST | `/api/admin/import/tmp` | `var/tmp/`의 곡 폴더와 zip을 등록하는 작업 시작(등록하거나 사용 중지 곡이라 건너뛴 폴더와, 실패 없이 끝난 zip은 지움). 이미 돌고 있으면 그 작업을 반환 → 작업 |
| GET·POST | `/api/admin/notices` | 공지 목록(최근 순)·새 공지. 본문 `{title, body, level(info/warning), published, starts_at, ends_at}`(시각은 unix 초, null이면 제한 없음). 틀리면 `422` |
| PUT·DELETE | `/api/admin/notices/{id}` | 공지 수정(본문은 POST와 같음, 전체를 바꿈)·삭제 |
| POST | `/api/admin/policies/{privacy\|terms}` | 새 판 저장. 본문 `{body, effective_on}`(시행일 `YYYY-MM-DD`, KST, 생략하면 오늘) |
| GET | `/api/admin/policies/{privacy\|terms}/template` | 기본 템플릿 `{kind, body}` |
| DELETE | `/api/admin/policies/versions/{id}` | 판 삭제(모두 지우면 기본 템플릿을 보여줌) |
| GET | `/api/admin/import/jobs`, `/api/admin/import/jobs/{id}` | 작업(최근 순). `{id, kind, name, status(running/done/failed), total, done, songs, error}`, 곡마다 `{folder, song_id, new_song, charts, new_charts}`(기존 곡에 병합했으면 `added_files`·`conflicts` 경로 목록이 붙음) 또는 `{folder, error}`. 사용 중지한 곡이라 건너뛰었으면 `skipped: "unused"`와 그 곡 번호 |
