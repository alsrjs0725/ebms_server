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

## HTTP API

모든 오류 응답은 FastAPI 기본 형식인 JSON `{"detail": "<메시지>"}` 입니다. 인증은 없습니다.

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET | `/` | 웹 메인 페이지 (HTML) |
| GET | `/static/{path}` | 정적 파일 (CSS/JS/아이콘) |
| GET | `/api/charthash` | 차트 청크별 SHA-256 목록 |
| GET | `/api/files/chart/{chunk_id}` | 차트 청크 zip 다운로드 |
| GET | `/api/files/song/{chart_sha256}` | 차트가 속한 곡 zip 다운로드 |

### `GET /api/charthash`

차트 청크 번호와 해당 청크 zip의 SHA-256 맵을 반환합니다. 클라이언트는 이 값을 로컬 청크와 비교해 바뀐 청크만 다시 받으면 됩니다.

- 파라미터: 없음
- 응답 `200` (`application/json`): 키는 청크 번호(JSON이라 문자열), 값은 SHA-256 hex. 청크가 없으면 `{}`.

```json
{"0": "3f1c...e9", "1": "a7b2...04"}
```

### `GET /api/files/chart/{chunk_id}`

차트 파일(`.bms`, `.bme`, `.bml`, `.pms`)을 묶은 청크 zip(무압축)을 내려줍니다. 청크는 64MB를 넘으면 다음 번호로 넘어가며, 마지막 청크는 새 곡이 추가될 때마다 내용과 해시가 바뀝니다.

- 경로 파라미터: `chunk_id` (int, 0부터 시작)
- 응답 `200` (`application/zip`): `chart_chunk_{chunk_id:05d}.zip` (예: `chart_chunk_00000.zip`)
- 오류
  - `404` `File not found`: 없는 청크 번호
  - `422`: `chunk_id`가 정수가 아님

### `GET /api/files/song/{chart_sha256}`

차트 파일의 SHA-256으로 그 차트가 속한 곡 폴더 전체(키음, BGA 등 포함) zip을 내려줍니다.

- 경로 파라미터: `chart_sha256` (string, 차트 파일 내용의 SHA-256 hex 소문자 64자)
- 응답 `200` (`application/zip`): 곡 zip (Deflate 압축, 폴더 구조 유지)
- 오류
  - `404` `chart file not found`: 등록되지 않은 차트 해시
  - `404` `chart file found but song file not found. report this to admin.`: 차트는 있으나 곡 데이터가 없음 (서버 데이터 불일치)

### 곡 등록

곡 등록용 HTTP API는 없습니다. 서버 시작 시 `var/tmp/` 아래 폴더를 훑어 차트 파일이 있는 폴더를 곡으로 등록하고, 등록한 폴더는 삭제합니다. 같은 차트(SHA-256과 크기 동일)가 이미 있으면 기존 곡에 연결됩니다.
