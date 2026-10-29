# HTTP API

모든 오류 응답은 FastAPI 기본 형식인 JSON `{"detail": "<메시지>"}` 입니다. 로그인하지 않으면 `/api/version`, `/api/notices`(와 세션키를 받는 `/api/auth/client/token`) 외의 모든 API가 `401`입니다. 곡 목록(차트 해시, 매니페스트)도 마찬가지입니다.

API 버전 2에서 기존 공개 다운로드 API(`/api/charthash`, `/api/manifest/*`, `/api/files/*`)를 없앴습니다. [사전/플레이 API](download.md)를 쓰세요.

API 버전 3에서 사전 청크(`/api/pre/assethash`, `/api/pre/asset/{chunk_id}`)를 추가했습니다. 클라이언트는 동기화 때 곡들의 배너·프리뷰 등을 이것으로 미리 받습니다.

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET | `/` | 웹 메인 페이지 (HTML) |
| GET | `/static/{path}` | 정적 파일 (CSS/JS/아이콘) |
| GET | `/table/{표 이름}.html` | 공유용 난이도표 정적 페이지 (로그인 불필요, `src/ebms_server/table/`) |
| GET | `/privacy`, `/terms` | 개인정보 처리방침·이용약관 (HTML, 로그인 불필요). 지난 판은 `/privacy/{판 id}` |
| GET | `/api/version` | API 버전. `auth`에 로그인 가능한 OAuth 목록(예: `["google", "discord"]`) |
| GET | `/api/notices` | 지금 게시 중인 공지 (로그인 불필요) |
| GET | `/api/pre/charthash` | 차트 청크별 SHA-256 목록 |
| GET | `/api/pre/chart/{chunk_id}` | 차트 청크 zip 다운로드 |
| GET | `/api/pre/assethash` | 사전 청크별 SHA-256 목록 |
| GET | `/api/pre/asset/{chunk_id}` | 사전 청크 zip 다운로드 |
| GET | `/api/pre/sizes` | 차트·사전 청크별 SHA-256과 바이트 크기. `{"chart": {id: {"sha256", "size"}}, "asset": {…}}` |
| GET | `/api/pre/tables` | 난이도표 목록과 사전 청크별 난이도표 태그 |
| GET | `/api/pre/manifest/hash` | 매니페스트 청크별 SHA-256 목록 |
| GET | `/api/pre/manifest/{chunk_id}` | 곡 매니페스트 (JSON) |
| GET | `/api/pre/song/{song_id}/file?path=` | 곡의 사전 파일 하나 |
| GET | `/api/play/song/{song_id}` | 곡 zip 다운로드 (티켓) |

## 파일 다운로드 공통 헤더

`/api/pre/chart/...`, `/api/pre/asset/...`, `/api/play/song/...` 응답은 모두 아래를 지원합니다.

- `X-Content-SHA256`, `ETag: "<sha256>"`: 파일 전체의 SHA-256 (부분 응답에도 전체 기준 값)
- `Range: bytes=a-b` / `bytes=a-` / `bytes=-n` (단일 범위): `206 Partial Content` + `Content-Range`. 범위 밖이면 `416` + `Content-Range: bytes */<크기>`. 여러 범위는 무시하고 `200`으로 전체를 보냅니다.
- `If-Range: "<sha256>"`: 값이 다르면 `Range`를 무시하고 전체를 보냅니다 (이어받기 중 파일이 바뀐 경우).
- `If-None-Match: "<sha256>"`: 같으면 `304`.

## `GET /api/version`

```json
{"api": 3, "server": "0.1.0", "auth": ["google", "discord"]}
```

`api`는 호환되지 않는 변경이 있을 때 올라갑니다. `server`는 알 수 없으면 `null`입니다. 로그인 없이 쓸 수 있는 유일한 API라, 클라이언트는 서버를 추가할 때 이것으로 호환 여부를 확인합니다.

## `GET /api/notices`

관리자가 `/admin/notices`에서 쓴 공지 중 지금 보여줄 것(게시 중이고 `starts_at` ≤ 지금 < `ends_at`)을 최근에 만든 순으로 반환합니다. 로그인 없이 씁니다. 클라이언트는 켜질 때 이것을 받아 아직 확인하지 않은 공지를 띄웁니다. `(id, updated_at)`이 같으면 같은 공지로 보면 됩니다.

```json
[{"id": 3, "title": "점검 안내", "body": "오늘 밤 2시~4시 점검합니다.", "level": "warning", "starts_at": null, "ends_at": 1791561600, "updated_at": 1791500000}]
```

- `level`: `info`(안내) 또는 `warning`(중요)
- `starts_at`, `ends_at`, `updated_at`: unix 초. `starts_at`·`ends_at`이 `null`이면 제한 없음
- `body`는 일반 텍스트입니다(HTML 아님).

## `GET /api/pre/charthash`

차트 청크 번호와 해당 청크 zip의 SHA-256 맵을 반환합니다. 클라이언트는 이 값을 로컬 청크와 비교해 바뀐 청크만 다시 받으면 됩니다.

- 파라미터: 없음
- 응답 `200` (`application/json`): 키는 청크 번호(JSON이라 문자열), 값은 SHA-256 hex. 청크가 없으면 `{}`.

```json
{"0": "3f1c...e9", "1": "a7b2...04"}
```

## `GET /api/pre/chart/{chunk_id}`

차트 파일(`.bms`, `.bme`, `.bml`, `.pms`, `.bmson`)을 묶은 청크 zip(무압축)을 내려줍니다. 청크마다 차트 100개(넘지 않더라도 64MB를 넘으면 그 전에)를 담고 다음 번호로 넘어가며, 마지막 청크는 새 곡이 추가될 때마다 내용과 해시가 바뀝니다.

zip 안 항목 이름은 `{차트 sha256}{확장자 소문자}` (예: `3f1c...e9.bms`)입니다. 원래 파일명과 소속 곡은 매니페스트에서 확인합니다.

- 경로 파라미터: `chunk_id` (int, 0부터 시작)
- 응답 `200` (`application/zip`): `chart_chunk_{chunk_id:05d}.zip` (예: `chart_chunk_00000.zip`)
- 오류
  - `404` `File not found`: 없는 청크 번호
  - `422`: `chunk_id`가 정수가 아님

## `GET /api/pre/assethash`

사전 청크 번호와 SHA-256 맵. 형식은 `/api/pre/charthash`와 같습니다.

## `GET /api/pre/asset/{chunk_id}`

곡들의 사전 파일(`kind`가 `pre`인 파일 중 차트를 뺀 것: 배너·스테이지파일·프리뷰 등)을 묶은 청크 zip(무압축)을 내려줍니다. 청크 `n`에는 `song_id`가 `n*8` 이상 `(n+1)*8` 미만인 곡이 들어가고, 그 구간에 곡이 추가되면 해당 청크만 다시 만들어집니다. 사전 파일이 없는 구간도 빈 zip이 있습니다.

zip 안 항목 이름은 `{song_id}/{곡 zip 안 경로}` (예: `123/banner.png`)이고, 내용·크기·crc32는 매니페스트 `files`의 항목과 같습니다.

- 응답 `200`/`206` (`application/zip`): `pre_chunk_{chunk_id:05d}.zip`
- 오류: `404` `File not found`, `422` `chunk_id`가 정수가 아님

이전 버전에서 등록한 곡의 사전 청크는 서버를 시작한 뒤 뒤에서 만듭니다. 아직 없는 곡은 `/api/pre/song/{song_id}/file`로 받으면 됩니다.

## `GET /api/pre/tables`

관리자가 등록한 난이도표와, 사전 청크마다 붙은 난이도표 태그입니다. 사전 청크에 그 난이도표의 곡(차트의 md5나 sha256이 표에 실린 곡)이 하나라도 있으면 그 표의 id가 태그로 붙습니다. 클라이언트는 사용자가 고른 표의 태그가 붙은 청크만 받으면 됩니다. 태그는 요청할 때 계산하므로 곡이 추가되거나 표가 바뀌면 다음 요청부터 반영됩니다.

서버는 속한 난이도표 조합이 같은 곡끼리 사전 청크에 묶으므로(한 곡이 표 여러 개에 있으면 그 조합의 청크에 들어가고 태그가 여러 개 붙음), 표를 고른 클라이언트는 고른 표에 없는 곡의 사전 파일을 거의 받지 않습니다. 표를 더하거나 다시 읽어 조합이 바뀐 곡은 서버가 뒤에서 조금씩(`EBMS_PRE_REBALANCE_BYTES`, 기본 2GiB씩 `EBMS_PRE_REBALANCE_PAUSE_SECONDS`, 기본 600초 간격) 새 청크로 옮기고, 곡이 옮겨진 청크는 해시가 바뀌어 다시 받게 됩니다. 곡이 다른 청크로 옮겨져도 청크 안 항목 이름(`{song_id}/{경로}`)은 그대로입니다. 차트 청크·매니페스트는 표와 상관없이 모두 받습니다.

```json
{
  "tables": [
    {"id": 1, "name": "発狂BMS難易度表", "symbol": "★", "url": "https://...", "charts": 3000, "matched_charts": 1200, "songs": 1100, "fetched_at": 1791315327}
  ],
  "chunks": {"0": [1, 2], "5": [1]}
}
```

- `charts`: 표에 실린 차트 수, `matched_charts`: 그중 서버에 있는 차트 수, `songs`: 그 차트가 든 곡 수, `fetched_at`: 표를 마지막으로 읽은 시각(unix 초)
- `chunks`: 사전 청크 번호 → 태그(표 id 목록). 난이도표의 곡이 없는 청크는 빠집니다.
- 이 경로가 없는 이전 서버(`404`)라면 클라이언트는 전곡을 받습니다. 사용량에는 세지 않습니다.

## `GET /api/pre/manifest/hash`

매니페스트 청크 번호와 SHA-256 맵. 형식은 `/api/pre/charthash`와 같습니다. 해시는 **압축을 푼 JSON** 기준입니다.

## `GET /api/pre/manifest/{chunk_id}`

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
- `preview`(있을 때만): 서버가 만든 미리듣기 음원 `{"path", "size", "crc32"}`. 곡 zip에 없는 사전 파일이라 `files`에는 없고, 사전 청크와 `/api/pre/song/{song_id}/file?path=`로 받습니다([다운로드 문서](download.md)).
- `files`: 곡 zip 안의 파일(디렉터리 제외). `offset`은 local file header 위치, `method`는 zip 압축 방식(0 무압축, 8 Deflate), `kind`는 사전(`pre`)/플레이(`play`) 구분입니다. 파일 하나만 받으려면 `offset`부터 30바이트를 받아 파일명/extra 길이(26~29바이트)를 읽고, 그 뒤 `comp_size` 바이트를 `Range`로 받습니다.

## `GET /api/play/song/{song_id}`

곡 폴더 전체(키음, BGA 등 포함) zip을 내려줍니다. song id는 매니페스트에서 얻습니다. 티켓 규칙은 [다운로드 문서의 "플레이 티켓"](download.md)을 보세요.

- 응답 `200`/`206` (`application/zip`): 곡 zip (Deflate 압축, 폴더 구조 유지)
- 오류: `404` `song not found`, `410` `song deleted` 또는 `song deleted: <삭제 사유>`(관리자가 지운 곡, 번호는 결번), `422` `song_id`가 정수가 아님, `429` `no download ticket`

## `POST /api/mcp` (MCP)

AI 클라이언트(agy, Claude 등)용 [MCP](https://modelcontextprotocol.io) 서버입니다(Streamable HTTP, JSON-RPC 2.0). 요청마다 JSON 하나로 답하고 서버가 먼저 보내는 스트림은 없어 `GET /api/mcp`는 405입니다.

- 인증: `Authorization: Bearer`로 클라이언트 세션키(사용자)나 관리자 API 키(`ebms_ak_...`)를 보냅니다. 웹 세션 쿠키로는 쓰지 못합니다(401).
- 권한은 사이트와 같은 두 단계입니다. `tools/list`는 그 권한으로 쓸 수 있는 도구만 보여 주고, 권한 밖의 도구를 부르면 JSON-RPC 오류 `-32003`입니다.

| 도구 | 권한 | 설명 |
| --- | --- | --- |
| `whoami` | 사용자 | 이 연결의 사용자 `{id, nickname, role(user/admin)}` |
| `list_conflict_units` | 관리자 | 검토 대기 묶음에서 병합 충돌이 남은 곡. `pending_only`(기본 true)면 AI 추천이 덜 된 곡만 |
| `get_conflict_case` | 관리자 | 곡 하나의 판단 자료: 겨룬 기존 곡과 차트, 새 차트의 상태(`same_as_registered`/`conflict`/`new_chart`), 충돌 파일마다 비교 수치(키음 상관계수·싱크 오차, 그림 차이, 영상 프레임 차이, 품질, 차트 diff 앞부분·`#WAV` 번호 바뀜)와 그 파일을 쓰는 차트(`used_by_old/new`, 차트면 `conflict_sounds_old/new`). `offset`·`limit`(기본 20, 최대 50)으로 나눠 받습니다. 곡 정보(`existing_songs`·`new_charts`)는 `offset` 0 응답에만 있고, 값이 없는 항목은 뺍니다. 비교 계산이 30초를 넘으면 그때까지 계산한 것만 주므로(앞단 프록시 시간 제한) `next_offset`으로 이어 받습니다. 계산한 비교는 캐시돼 다시 부르면 빠릅니다 |
| `get_file_diff` | 관리자 | 텍스트 충돌 파일의 전체 diff(최대 2000줄) |
| `get_sound_clip` | 관리자 | 키음 충돌의 기존·새 소리를 같은 구간(`start`초부터 `seconds`초, 기본 10초·최대 30초)으로 잘라 MCP content로 줍니다: 오디오(`audio/mpeg`, 192kbps)와 스펙트로그램(`image/png`). `audio`·`spectrogram`을 false로 하면 뺍니다. 오디오를 못 받는 모델은 스펙트로그램을 봅니다 |
| `submit_recommendation` | 관리자 | 충돌 파일마다 `old`/`new`/`unsure` 추천(`confidence`, `reason`)과 곡 전체 판단(`summary`, `split_recommended`, `issues`, `model`)을 저장합니다 |

추천은 묶음의 곡 정보(`unit.ai`)에 따로 저장하고 관리자가 고른 것(`resolutions`)은 바꾸지 않습니다. 충돌 해결 창에 파일마다 AI 추천과 이유가 보이고, **AI 추천대로 고르기**를 누르면 고르지 않은 파일 중 old·new로 추천된 것만 고릅니다(unsure는 직접 고름). 적용은 관리자가 합니다. 판단 기준은 `initialize` 응답의 `instructions`로 클라이언트에 알려 줍니다.

agy 등록 예(관리자 API 키는 관리자 페이지의 API 키에서 만듭니다):

```
agy mcp add --header "Authorization: Bearer ebms_ak_..." ebms https://<서버 주소>/api/mcp
```
