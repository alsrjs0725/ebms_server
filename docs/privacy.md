# 개인정보 보유기간과 자동 파기

서버는 보유기간이 지난 개인정보를 스스로 지웁니다(개인정보 보호법 제21조). 시작할 때 한 번, 이후 `EBMS_RETENTION_INTERVAL_HOURS`마다 정리 작업이 돌고, 지운 행 수를 로그에 남깁니다(`retention purge: {...}`). 코드: `src/ebms_server/retention.py`.

보유기간은 env로 바꿉니다. **값을 바꾸면 개인정보 처리방침(`/admin/policies`)의 보유기간도 같이 고치세요.**

| 데이터 | 테이블 | 기본 보유기간 | env |
| --- | --- | --- | --- |
| 만료·해지된 로그인 세션(종류, 사용 시각) | `session` | 만료 또는 해지 후 **30일** | `EBMS_RETENTION_SESSION_DAYS` |
| 곡별 다운로드 기록(마지막 차감 시각) | `download_grant` | 재차감 면제 기간(`grant_seconds`, 기본 30분)이 끝나면 **바로** | `EBMS_RETENTION_GRANT_DAYS` (0: 바로, -1: 지우지 않음) |
| 월별 사전 다운로드 사용량 | `pre_usage` | 이번 달 포함 **12개월** | `EBMS_RETENTION_PRE_USAGE_MONTHS` |
| 오래 쓰지 않은 계정 | `user`와 연결된 모든 행 | **지우지 않음**(기본 0) | `EBMS_RETENTION_INACTIVE_USER_DAYS` |
| 클라이언트 로그인용 1회용 코드 | `auth_code` | 만료 후 1시간(새 코드를 만들 때 지움) | — |
| 탈퇴한 로그인 수단의 재가입 제한 키(OAuth 계정 ID의 HMAC) | `rejoin_block` | 탈퇴 후 **24시간**, 정지 중 탈퇴는 **정지가 끝날 때까지**(무기한 정지면 지우지 않음) | `EBMS_REJOIN_BLOCK_HOURS` |

- 사용 중인 세션은 지우지 않습니다. 웹 세션은 30일, 클라이언트 세션은 90일 동안 쓰지 않으면 만료되고, 그 뒤 위 기간이 지나면 지워집니다.
- `download_grant`는 면제 기간이 끝나면 다음 다운로드 때 어차피 다시 차감하므로 지워도 할당량 동작은 같습니다.
- 계정 정리를 켜면(`EBMS_RETENTION_INACTIVE_USER_DAYS`>0) 마지막 로그인과 마지막 세션 사용이 모두 그 일수보다 오래된 계정을 지웁니다. 로그인 수단·세션·티켓·다운로드 기록·사용량은 외래 키로 함께 지워집니다. 관리자 계정은 지우지 않습니다. 되돌릴 수 없으니 처리방침에 먼저 알리고 켜세요.
- 탈퇴하면 계정 행은 바로 지우고, 재가입 제한 판별용으로 OAuth 계정 ID 원문 대신 DB에 둔 salt(`setting.rejoin_salt`)로 만든 HMAC만 남깁니다. 기간이 끝난 키와 끝난 정지(`user.banned_until`)는 정리 작업이 지우고 풉니다.
- `EBMS_RETENTION_INTERVAL_HOURS=0`이면 정리 작업을 끕니다(시작 로그에 경고).

## 서버 밖의 로그

DB 밖의 로그는 이 작업이 아니라 로그 회전으로 지워집니다. 보관 기간은 [deployment.md](deployment.md#로그-보관)(홈서버: 접근 로그 30일, 앱 로그 약 1년, 관리자 접속기록 1년 이상, docker 로그 10MB × 3개)와 [proxy.md](proxy.md#로그-보관)(오라클 VM: 최대 약 6주)에 있습니다.
