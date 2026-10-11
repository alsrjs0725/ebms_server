# 캐싱 리버스 프록시 (오라클 무료 VM 등)

홈서버를 직접 공개하지 않고, 외부 VM의 nginx가 TLS·접속 제한·캐시를 맡습니다. 큰 파일(곡 zip·차트 청크·사전 청크)은 VM의 캐시에서 나가므로 홈서버 회선과 DB 부하가 줄고, 홈서버 IP는 드러나지 않습니다.

```
클라이언트 ──https──▶ VM nginx (TLS, IP별 제한, 캐시) ──터널(Tailscale)──▶ 홈서버 EBMS :8000
```

## 권한 확인과 캐시

캐시된 파일도 요청마다 홈서버에 권한을 묻습니다. 로그인하지 않았거나 티켓이 없는 사람은 캐시에 있어도 받을 수 없습니다.

1. 클라이언트가 `/api/pre/chart/{id}`, `/api/pre/asset/{id}`, `/api/play/song/{id}`를 요청합니다.
2. nginx가 본문을 보내기 전에 홈서버의 `/api/proxy/authz`에 원래 요청의 `Authorization`·쿠키·`Range`·`If-None-Match`를 넘겨 묻습니다(`auth_request`). 홈서버는 로그인을 확인하고, 곡이면 티켓을 쓰고, 사전 청크면 사용량을 더한 뒤 그 파일의 sha256을 알려줍니다.
3. 거부면 원래 응답(`401`, `404`, `429` + `Retry-After`)을 그대로 돌려줍니다. 통과면 `경로|sha256`을 키로 캐시에서 보냅니다.
4. 캐시에 없으면 nginx가 같은 경로를 비밀값(`X-Ebms-Proxy-Secret`)과 함께, 사용자 정보는 빼고 요청해 채웁니다. 홈서버는 비밀값이 맞으면 인증·차감 없이 본문만 보냅니다(차감은 2에서 끝남).

- 키에 sha256이 들어가므로 곡이 바뀌면 새로 받고, 옛 항목은 `inactive`(기본 30일) 동안 쓰이지 않으면 지워집니다. 캐시 크기는 `EBMS_CACHE_MAX_SIZE`(기본 20g)를 넘지 않게 오래된 것부터 지웁니다.
- 같은 파일을 여러 명이 처음 동시에 받아도 홈서버에서는 한 번만 받습니다(`proxy_cache_lock`).
- 매니페스트, 해시 목록, 사전 파일 하나(`/api/pre/song/{id}/file`), 로그인·관리자 페이지는 캐시하지 않고 그대로 넘깁니다.
- 비밀값 헤더는 클라이언트가 보내도 nginx가 지우거나 덮어씁니다. 홈서버는 터널로만 열려 있어 VM 밖에서는 닿지 않습니다.

달라지는 점(프록시를 거칠 때만):

- 사전 청크 사용량은 실제로 보낸 바이트가 아니라 **요청한 바이트**(Range면 그 길이)를 요청 때 더합니다. 중간에 끊긴 다운로드도 다 받은 것으로 셉니다.
- 월 한도를 넘은 사용자의 감속은 연결마다 `pre_throttled_kbps`입니다. 같은 사용자의 동시 요청이 속도를 나눠 쓰지 않습니다.
- 사용자별 동시 다운로드 수 제한(10) 대신 nginx의 IP별 동시 연결 제한(32)을 씁니다.

## 접속 제한(DDoS 완화)

`deploy/proxy/nginx/`의 기본값입니다. 필요하면 VM의 `/etc/nginx/conf.d/ebms-common.conf`와 사이트 설정을 고칩니다.

| 대상 | IP당 제한 |
| --- | --- |
| 전체 동시 연결 | 32 |
| 큰 파일 요청 | 초당 10개(순간 200) |
| 로그인·세션 발급(`/auth/`, `/login`, `/api/auth/`) | 분당 30개(순간 20) |
| 나머지 | 초당 20개(순간 100) |

넘으면 `429`입니다. 느린 연결로 버티는 공격에 대비해 헤더·본문 대기 시간을 짧게 둡니다. 대역폭을 채우는 대규모 공격은 VM 한 대로 막을 수 없으니, 그 경우에는 VM 앞에 Cloudflare 같은 프록시를 더 두는 것을 고려하세요.

## 설치

### 1. 홈서버와 VM 연결(Tailscale)

홈서버 포트를 인터넷에 열지 않고 VM에서만 닿게 합니다.

1. 홈서버와 VM에 각각 설치하고 같은 계정으로 로그인합니다.

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```

2. 홈서버에서 EBMS 포트를 tailnet에만 엽니다. EBMS는 지금처럼 `127.0.0.1`에 바인딩된 채로 둡니다. stage도 쓰면 8001도 같은 식으로 엽니다.

   ```bash
   sudo tailscale serve --bg --tcp 8000 tcp://127.0.0.1:8000
   sudo tailscale serve --bg --tcp 8001 tcp://127.0.0.1:8001   # stage
   ```

3. 홈서버의 tailnet 주소를 확인합니다(`tailscale ip -4`, 예: `100.64.0.10`). VM에서 `curl http://100.64.0.10:8000/api/version`이 응답하면 됩니다.

### 2. 홈서버 env

`/opt/ebms/release.env`(stage는 `stage.env`)에 추가하고 다시 배포하거나 `docker compose ... up -d`로 올립니다.

```bash
EBMS_PUBLIC_URL=https://ebms.example.com     # VM의 도메인
EBMS_PROXY_SECRET=<openssl rand -hex 32 결과>  # 단계마다 다른 값
EBMS_FORWARDED_ALLOW_IPS=<docker 브리지 게이트웨이>
```

`tailscale serve`가 호스트의 `127.0.0.1`로 넘기므로, 컨테이너에서는 호스트 프록시와 같이 docker 브리지 게이트웨이에서 온 요청으로 보입니다. 확인: `docker network inspect ebms-release_default --format '{{(index .IPAM.Config 0).Gateway}}'`. 이 값을 넣어야 로그와 로그인 기록에 실제 접속 IP가 남습니다([deployment.md](deployment.md#tls-리버스-프록시-필수)).

OAuth 앱의 리다이렉트 주소도 새 도메인(`https://ebms.example.com/auth/<oauth>/callback`)으로 바꿉니다([auth.md](auth.md)).

### 3. VM

1. 도메인의 A 레코드를 VM의 공인 IP로 둡니다.
2. 오라클 콘솔에서 VM 서브넷의 **Security List**(또는 NSG)에 TCP 80, 443 수신(0.0.0.0/0)을 추가합니다. VM 안의 iptables는 설치 스크립트가 엽니다.
3. VM(Ubuntu 22.04/24.04)에서 실행합니다.

   ```bash
   git clone https://github.com/alsrjs0725/ebms_server.git
   cd ebms_server/deploy/proxy
   sudo EBMS_DOMAIN=ebms.example.com EBMS_ORIGIN=100.64.0.10:8000 \
        EBMS_PROXY_SECRET=<release.env와 같은 값> EBMS_ACME_EMAIL=me@example.com ./setup.sh
   # stage도 쓰면
   sudo EBMS_NAME=stage EBMS_DOMAIN=stage.ebms.example.com EBMS_ORIGIN=100.64.0.10:8001 \
        EBMS_PROXY_SECRET=<stage.env와 같은 값> EBMS_ACME_EMAIL=me@example.com ./setup.sh
   ```

   nginx·certbot을 설치하고, Let's Encrypt 인증서를 받고(자동 갱신), 설정을 `/etc/nginx/`에 넣습니다. 다시 실행해도 됩니다. 캐시 크기는 `EBMS_CACHE_MAX_SIZE=40g`처럼 바꿉니다(부트 볼륨 여유보다 작게).

4. 확인

   ```bash
   curl https://ebms.example.com/api/version
   curl -i https://ebms.example.com/api/play/song/1        # 401
   ```

   로그인한 클라이언트로 같은 곡을 두 번 받으면 두 번째 응답 헤더에 `X-Cache-Status: HIT`가 붙습니다.

### 4. 클라이언트

클라이언트에 등록한 서버 주소를 새 도메인(`https://ebms.example.com`)으로 바꿉니다.

## 운영

- 비밀값을 바꾸면 홈서버 env와 VM 설정을 함께 바꿉니다(VM은 `setup.sh`를 새 값으로 다시 실행). 둘이 다르면 큰 파일 요청이 `500`이 됩니다.
- 캐시 비우기: `sudo rm -rf /var/cache/nginx/ebms/* && sudo systemctl reload nginx`
- 로그: `/var/log/nginx/access.log`, `/var/log/nginx/error.log`
- `EBMS_PROXY_SECRET`을 비우면 연동이 꺼지고 다운로드를 홈서버가 직접 처리합니다(`/api/proxy/authz`는 404).
