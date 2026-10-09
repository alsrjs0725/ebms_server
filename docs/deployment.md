# 배포·운영

## 포트와 데이터

| 포트 | 서비스 | 외부 공개 | 비고 |
| --- | --- | --- | --- |
| 443/tcp | TLS 리버스 프록시 (HTTPS) | 필요 | Caddy·nginx 등. 외부에 여는 유일한 포트. 아래 [TLS 리버스 프록시](#tls-리버스-프록시-필수) 참고 |
| 8000/tcp (`EBMS_PORT`) | EBMS 서버 (HTTP) | 금지 | 호스트의 `127.0.0.1`에만 바인딩. 같은 호스트의 리버스 프록시만 접근 |
| 3306/tcp | MySQL | 불필요 | 호스트에 바인딩하지 않음. compose 내부 네트워크에서 `ebms` 컨테이너만 접근 |

- 곡/차트 데이터는 MySQL(`mysql-data` 볼륨)에 BLOB으로 저장되며, 테이블은 서버가 시작할 때 자동 생성됩니다.
- 서버 로그와 임포트 대기 폴더(`var/log`, `var/tmp`)는 `ebms-var` 볼륨에 유지됩니다.

## TLS 리버스 프록시 (필수)

EBMS 서버는 평문 HTTP만 말합니다. 웹 세션 쿠키(30일), OAuth 콜백 코드, 클라이언트 세션키(90일, `Authorization: Bearer`)가 오가므로 **외부에는 반드시 TLS 리버스 프록시(https)를 거쳐 공개**합니다.

- `docker-compose.yml`은 서버 포트를 `127.0.0.1:${EBMS_PORT}`에만 엽니다. 같은 호스트의 프록시가 `http://127.0.0.1:<EBMS_PORT>`로 넘기게 하세요.
- `EBMS_PUBLIC_URL`은 `https://` 주소로 둡니다. 그래야 세션 쿠키에 `Secure`가 붙고 OAuth 리다이렉트 주소도 https가 됩니다. localhost가 아닌 `http://` 주소면 서버가 시작할 때 경고 로그를 남깁니다.
- 프록시 헤더(`X-Forwarded-For`·`X-Forwarded-Proto`)는 `EBMS_FORWARDED_ALLOW_IPS`(쉼표 구분, 기본 `127.0.0.1`)에서 온 요청만 믿습니다. 호스트의 프록시가 게시된 포트로 접속하면 컨테이너에서는 **docker 브리지 게이트웨이 주소**(예: `172.18.0.1`)로 보이므로, 그 주소를 넣어야 실제 접속 IP·https가 반영됩니다. 확인: `docker network inspect <프로젝트>_default --format '{{(index .IPAM.Config 0).Gateway}}'` (`<프로젝트>`는 `ebms-release` 등). `*`(전부 믿음)는 쓰지 마세요.

Caddy 예시(인증서 자동 발급):

```
ebms.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

nginx 예시:

```nginx
server {
    listen 443 ssl;
    server_name ebms.example.com;
    # ssl_certificate ... ; ssl_certificate_key ... ;
    client_max_body_size 64m;   # 곡 임포트 조각(최대 32MB)보다 크게
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;    # 큰 다운로드를 바로 흘려보냄
    }
}
```

> **호환성 주의(이전 배포에서 올릴 때)**: 예전 `docker-compose.yml`은 8000 포트를 모든 인터페이스에 열었습니다. 이제는 `127.0.0.1`에만 열리므로, 클라이언트나 브라우저가 `http://<서버>:8000`으로 직접 접속하던 배포는 업데이트 후 접속되지 않습니다. 리버스 프록시를 먼저 세우고 `EBMS_PUBLIC_URL`을 `https://` 주소로 바꾼 뒤(OAuth 앱의 리다이렉트 주소도 함께) 올리세요. 클라이언트에 등록한 서버 주소도 https 주소로 바꿔야 합니다.

### 데이터를 다른 디스크(HDD)에 두기

`.env`에 절대 경로를 주면 docker 볼륨 대신 그 폴더에 저장합니다. 단계별로 다른 폴더를 써야 합니다.

```bash
# /opt/ebms/release.env
EBMS_MYSQL_DIR=/mnt/hdd/ebms/release/mysql
EBMS_VAR_DIR=/mnt/hdd/ebms/release/var
```

- 폴더가 없으면 docker가 만들고, MySQL 컨테이너가 소유자를 맞춥니다. HDD는 부팅 시 자동 마운트(`/etc/fstab`)되게 해 두세요. 마운트 전에 컨테이너가 뜨면 빈 폴더에 새 DB가 생깁니다.
- 이미 docker 볼륨에 데이터가 있으면 옮긴 뒤 경로를 바꿉니다.

  ```bash
  docker compose -p ebms-release --env-file /opt/ebms/release.env down
  sudo mkdir -p /mnt/hdd/ebms/release/mysql
  docker run --rm -v ebms-release_mysql-data:/from -v /mnt/hdd/ebms/release/mysql:/to alpine cp -a /from/. /to/
  # release.env에 EBMS_MYSQL_DIR 추가 후 다시 up
  ```

## 브랜치와 자동 배포

| 브랜치 | 단계 | 병합 시 |
| --- | --- | --- |
| `develop` | DEVELOP | 테스트만 실행 |
| `stage` | STAGE | 테스트 후 홈서버에 `ebms-stage`로 배포 |
| `release` | RELEASE | 테스트 후 홈서버에 `ebms-release`로 배포 |

작업은 `develop`으로 PR을 보내고, `develop` → `stage` → `release` 순서로 올립니다. `stage`·`release`에 병합되면 `.github/workflows/deploy.yml`이 이미지를 빌드해 GHCR(`ghcr.io/alsrjs0725/ebms_server`)에 올리고(`stage` / `latest`·`release`·`<버전>` 태그, 커밋마다 `sha-<커밋>`), 홈서버의 self-hosted runner가 그 커밋의 이미지를 받아 `docker compose -p ebms-<단계> --env-file <단계>.env up -d`로 띄웁니다. 두 단계는 compose 프로젝트 이름이 달라 컨테이너와 볼륨(MySQL 데이터 포함)이 따로 유지됩니다.

### 홈서버 초기 세팅 (한 번만)

1. Docker와 Compose 플러그인을 설치합니다.
2. runner를 컨테이너로 띄웁니다. 호스트의 docker 소켓을 받아 EBMS 컨테이너를 띄우고, 재부팅하면 docker가 자동으로 다시 시작합니다(root 계정만 있어도 됩니다).
   1. GitHub 저장소 **Settings → Actions → Runners → New self-hosted runner**에서 `--token` 뒤의 값을 복사합니다(1시간 안에 사용).
   2. 홈서버에서 실행합니다.

      ```bash
      git clone https://github.com/alsrjs0725/ebms_server.git /opt/ebms-src
      cd /opt/ebms-src/deploy/runner
      RUNNER_TOKEN=<복사한 토큰> docker compose up -d --build
      docker compose logs -f   # "Listening for Jobs"가 보이면 완료
      ```

   등록 정보는 `runner` 볼륨에 남으므로 이후 재시작에는 토큰이 필요 없습니다. 다시 등록하려면 `docker compose down -v` 후 새 토큰으로 올립니다. compose 프로젝트 이름은 `ebms-runner`로 고정돼 있어 다른 runner 컨테이너(`runner-runner-1` 등)와 겹치지 않습니다. 이 파일로 다른 저장소용 runner를 하나 더 띄우려면 `RUNNER_URL`, `RUNNER_NAME`, `RUNNER_LABELS`를 바꾸고 `-p <다른 이름>`을 붙입니다.

   > runner 컨테이너는 docker 소켓을 쓰므로 호스트 root와 같은 권한을 가집니다. 이 저장소의 워크플로만 실행되도록 runner는 저장소 단위로 등록합니다.

3. 단계별 env 파일을 `/opt/ebms/`에 만듭니다(위치는 저장소 변수 `EBMS_ENV_DIR`로 변경 가능). `.env.example`을 복사해 값을 채우되, **포트·주소·비밀번호는 단계마다 다르게** 둡니다.

   ```bash
   mkdir -p /opt/ebms
   cp /opt/ebms-src/.env.example /opt/ebms/release.env   # EBMS_PORT=8000
   cp /opt/ebms-src/.env.example /opt/ebms/stage.env     # EBMS_PORT=8001
   chmod 600 /opt/ebms/*.env
   ```

   OAuth 앱의 리다이렉트 주소도 단계별 `EBMS_PUBLIC_URL`에 맞춰 각각 등록합니다([auth.md](auth.md)).

4. 처음 한 번은 저장소 **Actions → Deploy → Run workflow**로 `stage`, `release`를 각각 실행해 확인합니다.

수동으로 다루려면 같은 이름을 씁니다: `docker compose -p ebms-stage --env-file /opt/ebms/stage.env logs -f ebms`.

## 곡 등록

관리자 페이지의 **곡 임포트**(`/admin/import`)에서 zip을 올려 등록합니다([admin.md](admin.md)). 차트 파일이 있는 폴더를 곡으로 등록하고, 같은 차트(SHA-256과 크기 동일)가 이미 있으면 기존 곡에 연결됩니다.

서버에 직접 넣으려면 `var/tmp/` 아래에 곡 폴더나 zip을 두고 서버를 재시작하거나 곡 임포트 페이지의 **var/tmp 가져오기**를 누릅니다. 등록한 곡 폴더는 삭제하고, zip은 모든 곡을 등록했을 때만 삭제합니다. 서버 시작 시 임포트는 백그라운드로 돌아 시작을 막지 않습니다.

수 GB 이상의 곡모음집은 웹 업로드가 앞단 프록시(외부 주소)를 거쳐 느릴 수 있습니다. 같은 내부망이면 zip을 `var` 볼륨의 `tmp/`(`EBMS_VAR_DIR/tmp`)에 SMB·scp 등으로 복사한 뒤 **var/tmp 가져오기**를 누르는 편이 빠릅니다. 복사 도중에 가져오지 않도록 다른 이름(예: `songs.zip.part`)으로 복사한 뒤 `.zip`으로 바꾸세요.

큰 zip은 조각으로 나눠 올리므로 앞단 리버스 프록시의 요청 크기 제한을 바꾸지 않아도 됩니다. 서버(`var` 볼륨)에 zip 크기 + 2GB의 여유 공간이 필요합니다.

## 이전 버전 데이터 마이그레이션

서버가 시작할 때 없는 컬럼을 추가하고 기존 곡의 매니페스트를 채웁니다. 차트 청크의 항목 이름 변경은 한 번 직접 실행합니다 (청크 해시가 바뀌어 클라이언트가 다시 받습니다).

```bash
docker compose exec ebms python -m ebms_server.migrate
```
