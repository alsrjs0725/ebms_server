# HTTP API

모든 오류 응답은 FastAPI 기본 형식인 JSON `{"detail": "<메시지>"}` 입니다. 로그인하지 않으면 `/api/version`(과 세션키를 받는 `/api/auth/client/token`) 외의 모든 API가 `401`입니다. 곡 목록(차트 해시, 매니페스트)도 마찬가지입니다.

API 버전 2에서 기존 공개 다운로드 API(`/api/charthash`, `/api/manifest/*`, `/api/files/*`)를 없앴습니다. [사전/플레이 API](download.md)를 쓰세요.

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

## 파일 다운로드 공통 헤더

`/api/pre/chart/...`, `/api/play/song/...` 응답은 모두 아래를 지원합니다.

- `X-Content-SHA256`, `ETag: "<sha256>"`: 파일 전체의 SHA-256 (부분 응답에도 전체 기준 값)
- `Range: bytes=a-b` / `bytes=a-` / `bytes=-n` (단일 범위): `206 Partial Content` + `Content-Range`. 범위 밖이면 `416` + `Content-Range: bytes */<크기>`. 여러 범위는 무시하고 `200`으로 전체를 보냅니다.
- `If-Range: "<sha256>"`: 값이 다르면 `Range`를 무시하고 전체를 보냅니다 (이어받기 중 파일이 바뀐 경우).
- `If-None-Match: "<sha256>"`: 같으면 `304`.

## `GET /api/version`

```json
{"api": 2, "server": "0.1.0", "auth": ["google", "discord"]}
```

`api`는 호환되지 않는 변경이 있을 때 올라갑니다. `server`는 알 수 없으면 `null`입니다. 로그인 없이 쓸 수 있는 유일한 API라, 클라이언트는 서버를 추가할 때 이것으로 호환 여부를 확인합니다.

## `GET /api/pre/charthash`

차트 청크 번호와 해당 청크 zip의 SHA-256 맵을 반환합니다. 클라이언트는 이 값을 로컬 청크와 비교해 바뀐 청크만 다시 받으면 됩니다.

- 파라미터: 없음
- 응답 `200` (`application/json`): 키는 청크 번호(JSON이라 문자열), 값은 SHA-256 hex. 청크가 없으면 `{}`.

```json
{"0": "3f1c...e9", "1": "a7b2...04"}
```

## `GET /api/pre/chart/{chunk_id}`

차트 파일(`.bms`, `.bme`, `.bml`, `.pms`)을 묶은 청크 zip(무압축)을 내려줍니다. 청크는 64MB를 넘으면 다음 번호로 넘어가며, 마지막 청크는 새 곡이 추가될 때마다 내용과 해시가 바뀝니다.

zip 안 항목 이름은 `{차트 sha256}{확장자 소문자}` (예: `3f1c...e9.bms`)입니다. 원래 파일명과 소속 곡은 매니페스트에서 확인합니다.

- 경로 파라미터: `chunk_id` (int, 0부터 시작)
- 응답 `200` (`application/zip`): `chart_chunk_{chunk_id:05d}.zip` (예: `chart_chunk_00000.zip`)
- 오류
  - `404` `File not found`: 없는 청크 번호
  - `422`: `chunk_id`가 정수가 아님

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
- `files`: 곡 zip 안의 파일(디렉터리 제외). `offset`은 local file header 위치, `method`는 zip 압축 방식(0 무압축, 8 Deflate), `kind`는 사전(`pre`)/플레이(`play`) 구분입니다. 파일 하나만 받으려면 `offset`부터 30바이트를 받아 파일명/extra 길이(26~29바이트)를 읽고, 그 뒤 `comp_size` 바이트를 `Range`로 받습니다.

## `GET /api/play/song/{song_id}`

곡 폴더 전체(키음, BGA 등 포함) zip을 내려줍니다. song id는 매니페스트에서 얻습니다. 티켓 규칙은 [다운로드 문서의 "플레이 티켓"](download.md)을 보세요.

- 응답 `200`/`206` (`application/zip`): 곡 zip (Deflate 압축, 폴더 구조 유지)
- 오류: `404` `song not found`, `422` `song_id`가 정수가 아님, `429` `no download ticket`
