# 다운로드 API (사전 / 플레이)

클라이언트 입장에서 다운로드는 두 단계입니다.
- **사전 다운로드(= 동기화)**: 차트 청크, 매니페스트, 사전 청크(배너·스테이지파일·프리뷰 등)를 바뀐 청크만 미리 받습니다.
- **플레이 다운로드**: 곡을 처음 플레이할 때 곡 zip 전체를 받습니다.

다운로드는 두 갈래이고 둘 다 로그인(Bearer 세션키 또는 웹 쿠키)이 필요합니다. 없거나 만료된 세션은 `401`입니다. 서버는 경로만 보고 어느 할당량에서 뺄지 정합니다.

| 구분 | 메서드·경로 | 설명 |
| --- | --- | --- |
| 사전 | GET `/api/pre/charthash` | 차트 청크별 SHA-256 |
| 사전 | GET `/api/pre/chart/{chunk_id}` | 차트 청크 zip (Range 지원) |
| 사전 | GET `/api/pre/manifest/hash`, `/api/pre/manifest/{chunk_id}` | 매니페스트. `files`의 각 항목에 `kind`(`pre`/`play`) |
| 사전 | GET `/api/pre/assethash`, `/api/pre/asset/{chunk_id}` | 사전 청크. 속한 난이도표 조합이 같은 곡을 8곡까지 묶어 사전 파일(차트 제외)을 담은 zip (Range 지원). 청크 번호는 곡 id와 무관합니다 |
| 사전 | GET `/api/pre/sizes` | 차트·사전 청크별 SHA-256과 크기. 클라이언트가 동기화 전에 받을 용량을 계산합니다 |
| 사전 | GET `/api/pre/song/{song_id}/file?path=<zip 안 경로>` | 사전 청크가 아직 없을 때 쓰는 대체 경로. 곡의 사전 파일 하나를 압축을 풀어 보냅니다. `ETag`/`If-None-Match` 지원. `kind`가 `pre`가 아니면 `403` `not a pre-download file`, 없으면 `404`, 지운 곡(결번)이면 `410` |
| 플레이 | GET `/api/play/song/{song_id}` | 곡 zip 전체(Range 이어받기 지원). 본문을 보낼 때 티켓 1개 |

**사전/플레이 판정**: 곡을 등록할 때(기존 곡에 파일을 더하면 다시) 정합니다. 다음은 `pre`, 나머지(키음, BGA 등)는 `play`입니다.
- 차트 파일
- 차트 헤더 `#BANNER`·`#STAGEFILE`·`#BACKBMP`(이미지), `#PREVIEW`(오디오)가 가리키는 파일. 헤더 값의 확장자가 그 형식이 아니면(확장자 없음 포함) 무시합니다. 헤더 경로는 대소문자·`\`를 무시하고, 실제 파일 확장자가 달라도 같은 형식(이미지끼리, 오디오끼리)이면 같은 파일로 봅니다.
- 차트와 같은 폴더에서 이름이 `preview`로 시작하는 오디오 파일. 단 `#WAVxx`·`#BMPxx`가 가리키는 파일(키음·BGA)은 뺍니다.

**서버가 만든 미리듣기**: 사전 파일인 오디오(preview 음원)가 없는 곡은 서버가 차트의 키음을 섞어 미리듣기 음원을 만듭니다. 곡 zip에는 없고, 매니페스트의 `preview` 항목(`path`·`size`·`crc32`)과 사전 청크(`{song_id}/{path}`), `/api/pre/song/{song_id}/file?path=<path>`로 받습니다. 경로는 가장 큰 차트가 든 폴더의 `preview_ebms.ogg`입니다.
- Qwilight는 곡 고르는 화면에서 미리듣기할 때 키음·BGA를 모두 읽습니다. preview 음원이 있는 곡은 그 음원만 재생하므로, 클라이언트는 이 파일을 곡 폴더에 보여 주고 그 직후 Qwilight가 읽는 플레이 파일은 받지 않습니다(티켓을 쓰지 않음).
- 저작권·용량 때문에 일부러 음질을 낮춥니다: 30초, 모노 22050Hz, Vorbis 최저 품질(약 30kbps, 곡당 100KB 안팎).
- Qwilight의 하이라이트 위치부터 자릅니다(곡을 100칸으로 나눠 플레이 노트 밀도가 가장 크게 오르는 곳, 곡 가운데일수록 가중). 30초가 곡 끝을 넘으면 앞당기고, 첫 소리보다 앞서지는 않습니다.
- 차트는 곡에서 가장 큰 차트 하나를 씁니다. `#RANDOM`은 항상 1이 나온 것으로 봅니다.
- 서버를 시작할 때와 곡을 등록한 뒤 뒤에서 만들고(ffmpeg 필요, `EBMS_PREVIEW=0`이면 끔), 만든 곡의 사전 청크·매니페스트를 다시 만듭니다. 곡 zip이 바뀌면 다시 만듭니다.

**플레이 티켓**: 곡 1개당 1개를 쓰고, 차감 후 `grant_seconds`(기본 30분) 동안 같은 곡은 다시 받아도(Range 이어받기 포함) 차감하지 않습니다. 티켓은 `refill_seconds`(기본 60초)마다 1개씩 `max_tickets`(기본 5개)까지 찹니다. 티켓이 없으면 `429` + `Retry-After: <다음 티켓까지 초>` + `{"detail": "no download ticket"}`입니다. `304`·`416`·`404`·`410`은 차감하지 않습니다.

**사전 다운로드 사용량**: 보낼 바이트(Range면 그 길이, 매니페스트는 gzip이면 압축된 크기)를 요청 때 월별로 더하므로 중간에 끊긴 다운로드도 다 받은 것으로 셉니다. 월 경계는 KST 1일 0시입니다. 이번 달 사용량이 `pre_monthly_bytes`(기본 10GB)를 넘었으면 끊지 않고 연결마다 `pre_throttled_kbps`(기본 500Kbps)로 감속합니다. 해시 목록(`/hash`, `/charthash`, `/assethash`, `/sizes`)은 세지 않습니다. 첫 동기화는 모든 곡의 사전 파일을 받으므로 곡이 많으면 한 번에 한도를 넘을 수 있고, 넘은 뒤에는 감속된 속도로 마저 받습니다.

캐싱 리버스 프록시([proxy.md](proxy.md)) 뒤에서도 규칙은 같습니다(차감·사용량은 프록시가 묻는 `/api/proxy/authz`에서 셈).

`/api/me`에 남은 양이 나옵니다.

```json
{
  "tickets": {"available": 3, "max": 5, "refill_seconds": 60, "next_refill_at": 1791315327, "next_refill_in": 42},
  "pre": {"month": "2026-10", "used_bytes": 123456, "limit_bytes": 10737418240, "throttled_kbps": 500, "throttled": false}
}
```

`next_refill_at`은 다음 티켓 1개가 차는 시각(unix 초), `next_refill_in`은 그때까지 남은 초이고, 가득 차 있으면 둘 다 `null`입니다. 클라이언트 시계는 서버와 어긋날 수 있으므로 남은 시간은 `next_refill_in`으로 계산하세요.
