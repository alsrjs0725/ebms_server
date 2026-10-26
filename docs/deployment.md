# 배포·운영

## 포트와 데이터

| 포트 | 서비스 | 외부 공개 | 비고 |
| --- | --- | --- | --- |
| 443/tcp | TLS 리버스 프록시 (HTTPS) | 필요 | Caddy·nginx 등. 외부에 여는 유일한 포트. 아래 [TLS 리버스 프록시](#tls-리버스-프록시-필수) 참고 |
| 8000/tcp (`EBMS_PORT`) | EBMS 서버 (HTTP) | 금지 | 기본은 호스트의 `127.0.0.1`에만 바인딩(`EBMS_BIND_ADDR`). 같은 호스트의 리버스 프록시만 접근 |
| 3306/tcp | MySQL | 불필요 | 호스트에 바인딩하지 않음. compose 내부 네트워크에서 `ebms` 컨테이너만 접근 |

- 곡 zip·차트 청크·사전 청크는 파일로 `var/store/`(`EBMS_STORE_DIR`)에 두고, MySQL(`mysql-data` 볼륨)에는 경로·크기·sha256 같은 메타데이터만 둡니다(#152). 테이블은 서버가 시작할 때 자동 생성됩니다. 백업할 때는 DB와 `var/store/`를 함께 백업합니다.
- 서버 로그, 임포트 폴더(`var/log`, `var/tmp`, 검토 대기 `var/staged`), 저장소(`var/store`)는 `ebms-var` 볼륨에 유지됩니다.

### 로그 보관

접속 IP처럼 개인정보가 담긴 로그는 아래 기간만 남기고 지웁니다. 바꾸면 개인정보 처리방침의 보유기간도 함께 고칩니다.

| 로그 | 위치 | 담기는 정보 | 보관 |
| --- | --- | --- | --- |
| HTTP 접근 로그 | `var/log/access.log` | 접속 IP, 시각, 메서드·경로, 상태코드 | 날마다 회전, **30일**(`constant.ACCESS_LOG_DAYS`) |
| 서버 앱 로그 | `var/log/ebms.log` | 사용자·관리자 id(오류, 곡 삭제·임포트 등) | 7일 단위 회전 × 53개, 약 1년 |
| 관리자 접속기록 | `var/log/audit.log` | 관리자 id, 접속 IP, 행위, 대상 사용자 id | 7일 단위 회전 × 53개, **1년 이상**([admin.md](admin.md)) |
| 컨테이너 stdout(`docker compose logs`) | docker json-file | 서버 앱 로그와 같은 내용(접근 로그는 없음) | 컨테이너마다 10MB × 3개(`docker-compose.yml`의 `x-logging`) |

- 접근 로그는 stdout(docker 로그)에 나가지 않습니다. 볼 때는 `docker compose exec ebms tail -f var/log/access.log`.
- 이 설정 전에 쌓인 docker 로그는 컨테이너를 다시 만들 때(`docker compose up -d`) 지워집니다.
- VM 프록시의 로그 보관은 [proxy.md](proxy.md#로그-보관)에 있습니다.

## TLS 리버스 프록시 (필수)

> 홈서버를 직접 공개하지 않고 외부 VM(오라클 무료 VM 등)에서 캐시·접속 제한까지 맡기려면 [proxy.md](proxy.md)를 따르세요. 아래는 같은 호스트에 프록시를 두는 경우입니다.

EBMS 서버는 평문 HTTP만 말합니다. 웹 세션 쿠키(30일), OAuth 콜백 코드, 클라이언트 세션키(90일, `Authorization: Bearer`)가 오가므로 **외부에는 반드시 TLS 리버스 프록시(https)를 거쳐 공개**합니다.

- `docker-compose.yml`은 서버 포트를 `${EBMS_BIND_ADDR}:${EBMS_PORT}`(기본 `127.0.0.1`)에만 엽니다. 같은 호스트의 프록시가 `http://127.0.0.1:<EBMS_PORT>`로 넘기게 하세요.
- **프록시가 docker 컨테이너인 경우**(nginx-proxy-manager, 컨테이너로 띄운 Caddy·nginx 등): 컨테이너 안의 `127.0.0.1`은 호스트가 아니므로 루프백 바인딩에 닿지 못합니다. 이때 서버는 healthy인데 웹페이지는 502가 납니다. `EBMS_BIND_ADDR`를 docker 브리지 게이트웨이(보통 `172.17.0.1`, 확인: `docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}'`)로 두고, 프록시는 `http://172.17.0.1:<EBMS_PORT>`로 넘기게 하세요. 이 주소는 외부 인터페이스가 아니어서 평문 포트가 밖에 드러나지 않습니다. `0.0.0.0`은 쓰지 마세요.
- `EBMS_PUBLIC_URL`은 `https://` 주소로 둡니다. 그래야 세션 쿠키에 `Secure`가 붙고 OAuth 리다이렉트 주소도 https가 됩니다. localhost가 아닌 `http://` 주소면 서버가 시작할 때 경고 로그를 남깁니다.
- 프록시 헤더(`X-Forwarded-For`·`X-Forwarded-Proto`)는 `EBMS_FORWARDED_ALLOW_IPS`(쉼표 구분, 기본 `127.0.0.1`)에서 온 요청만 믿습니다. 호스트의 프록시가 게시된 포트로 접속하면 컨테이너에서는 **docker 브리지 게이트웨이 주소**(예: `172.18.0.1`)로 보이므로, 그 주소를 넣어야 실제 접속 IP·https가 반영됩니다. 확인: `docker network inspect <프로젝트>_default --format '{{(index .IPAM.Config 0).Gateway}}'` (`<프로젝트>`는 `ebms-release` 등). `*`(전부 믿음)는 쓰지 마세요.
- 서버는 모든 응답에 CSP(`frame-ancestors 'none'` 포함)·`X-Content-Type-Options`·`X-Frame-Options`·`Referrer-Policy`를 붙입니다. HSTS(`Strict-Transport-Security`)는 붙이지 않으므로 필요하면 프록시에서 붙이세요(Caddy는 기본으로 붙이지 않음, nginx는 `add_header Strict-Transport-Security "max-age=31536000" always;`).

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

### SSD를 작업 공간으로 쓰기

곡은 모두 저장소(`var/store`)에 들어가므로 데이터는 큰 디스크(HDD)에 두고, 작은 SSD는 임포트 중간 작업과 MySQL 쓰기 로그에만 씁니다. SMR HDD에서는 압축 풀기·commit이 디스크를 다투어 곡 하나에 몇 분씩 걸릴 수 있습니다.

```bash
# /opt/ebms/release.env
EBMS_CACHE_DIR=/mnt/ssd/ebms/release/cache
EBMS_CACHE_MAX_BYTES=50G
EBMS_MYSQL_LOG_DIR=/mnt/ssd/ebms/release/mysql-log
```

- **임포트 캐시**(`EBMS_CACHE_DIR`, `EBMS_CACHE_MAX_BYTES`): 웹으로 올린 파일과, 풀어 둔 대기 묶음(commit 작업 폴더 포함)을 이 폴더에 둡니다. 미리듣기 임시 파일도 여기서 만듭니다. 새 업로드·묶음마다 크기(zip은 풀린 크기, rar·7z는 압축 파일의 2배로 추정)를 보고, 지금 쓰는 양(파일마다 디스크 할당 단위로 올림한 실제 사용량)과 합쳐 한도 안이고 디스크에 1GB 이상 남으면 캐시에, 아니면 지금처럼 `var/`에 둡니다. 대기 묶음은 commit하거나 버릴 때까지 한도에 들어갑니다. 관리자 페이지 **검토 대기**에 사용량이 나오고, 캐시에 둔 묶음에는 "캐시" 표시가 붙습니다. `EBMS_CACHE_MAX_BYTES=0`(기본)이면 끕니다. 껐다가 켜도 이미 만든 묶음은 있던 자리에서 그대로 찾습니다.
- **MySQL 쓰기 로그**(`EBMS_MYSQL_LOG_DIR`): binlog, redo log, doublewrite 파일을 이 폴더에 둡니다(테이블 데이터는 `EBMS_MYSQL_DIR`에 그대로). 몇 GB면 충분합니다. 비우면 데이터 폴더를 같은 자리에 붙이므로 지금까지와 같습니다.

  이미 데이터가 있는 DB에서 처음 켤 때는 MySQL을 멈추고 파일을 옮긴 뒤 켭니다. 옮기지 않고 켜면 redo log가 없어 MySQL이 뜨지 않습니다(데이터는 그대로이니 옮긴 뒤 다시 켜면 됩니다).

  ```bash
  docker compose -p ebms-release --env-file /opt/ebms/release.env stop mysql ebms
  DATA=/mnt/hdd/ebms/release/mysql LOG=/mnt/ssd/ebms/release/mysql-log
  sudo mkdir -p $LOG
  sudo mv "$DATA/#innodb_redo" "$DATA"/#ib_*.dblwr "$DATA"/binlog.* $LOG/
  # binlog 목록의 ./binlog.NNN(데이터 폴더 기준)을 새 위치로 바꿉니다.
  sudo sed -i 's#^\./#/var/lib/mysql-log/#' $LOG/binlog.index
  sudo chown -R --reference="$DATA" $LOG
  # release.env에 EBMS_MYSQL_LOG_DIR 추가 후 다시 up
  ```

  SSD도 부팅 시 자동 마운트되게 해 두세요. 마운트 전에 MySQL이 뜨면 redo log가 없어 시작하지 못합니다.

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

관리자 페이지의 **곡 임포트**(`/admin/import`)에서 zip·7z나 rar(분할 압축 포함)를 올리면 먼저 **검토 대기**(`var/staged/`)에 풀어 두고, 곡마다 넣을 곳을 확인·변경한 뒤 **commit**하면 등록합니다([admin.md](admin.md)). 차트 파일이 있는 폴더를 곡으로 보고, 자동으로 두면 같은 차트(SHA-256과 크기 동일)가 이미 있는 기존 곡에 연결됩니다. 같은 차트가 없고 곡에 없는 키음이 있으면(차분), 그 키음을 가지고 있고 `#TITLE`·`#ARTIST`·`#BPM`·`#GENRE` 중 하나 이상 같은 곡을 후보로 보여주고, 키음이 모두 갖춰지는 곡이 하나면 그 곡에 추가합니다(비슷한 곡 있음). 키음이 다 든 곡도 그 키음을 대부분 가진 기존 곡이 있으면 중복 의심 후보로 보여줍니다(자동 추가는 안 함).

- 기존 곡에 연결될 때 새 폴더에만 있는 파일(새 차트의 키음·BGA 등)은 그 곡 zip에 더합니다. 곡 zip·매니페스트·사전 청크가 다시 만들어져 클라이언트가 그 곡을 다시 받습니다.
- 같은 경로(대소문자 무시)에 내용이 다른 파일이 있으면 기존 파일을 그대로 두고 경고 로그와 결과의 `conflicts`에 남깁니다. 그 경로의 차트는 등록하지 않으며, 그 곡은 검토 대기에 남습니다.
- 새 폴더의 차트가 기존 곡 **여러 개**와 겹치면 검토 대기에서 "여러 곡과 겹침"으로 보입니다. 자동으로 두면 등록하지 않고 실패로 남으므로 넣을 곡을 직접 고르세요.

서버에 직접 넣으려면 `var/tmp/` 아래에 곡 폴더나 zip·rar·7z(rar 분할 압축은 볼륨 전부)를 두고 곡 임포트 페이지의 **var/tmp 가져오기**를 누릅니다(서버를 시작할 때는 가져오지 않습니다). 곡 폴더는 검토 대기로 옮기고, zip·rar·7z는 모든 곡을 풀었을 때만 삭제합니다. rar는 도커 이미지에 든 `unrar`(Debian non-free)로 풀며, 도커 없이 돌리면 `unrar`를 따로 설치해야 합니다(경로는 `EBMS_UNRAR`로 바꿀 수 있음). 7z는 도커 이미지에 든 7-Zip(`7zip` 패키지의 `7z`)으로 풀며, 도커 없이 돌리면 7-Zip을 설치해야 합니다(`7z`, 없으면 `7zz`를 찾고, `EBMS_7Z`로 바꿀 수 있음). 검토 대기의 곡은 commit하거나 폐기할 때까지 `var` 볼륨의 `staged/`에서 디스크를 씁니다.

수 GB 이상의 곡모음집은 웹 업로드가 앞단 프록시(외부 주소)를 거쳐 느릴 수 있습니다. 같은 내부망이면 zip을 `var` 볼륨의 `tmp/`(`EBMS_VAR_DIR/tmp`)에 SMB·scp 등으로 복사한 뒤 **var/tmp 가져오기**를 누르는 편이 빠릅니다. 복사 도중에 가져오지 않도록 다른 이름(예: `songs.zip.part`)으로 복사한 뒤 `.zip`·`.rar`·`.7z` 등 원래 이름으로 바꾸세요.

큰 zip은 조각으로 나눠 올리므로 앞단 리버스 프록시의 요청 크기 제한을 바꾸지 않아도 됩니다. 서버(`var` 볼륨)에 zip 크기 + 2GB의 여유 공간이 필요합니다.

## 이전 버전 데이터 마이그레이션

서버가 시작할 때 없는 컬럼을 추가하고 기존 곡의 매니페스트를 채웁니다. 차트 청크의 항목 이름 변경은 한 번 직접 실행합니다 (청크 해시가 바뀌어 클라이언트가 다시 받습니다).

```bash
docker compose exec ebms python -m ebms_server.migrate
```

### DB BLOB을 저장소 파일로 옮기기 (#152)

이전 버전은 곡 zip·청크를 MySQL BLOB(`song_part`, `chart_chunk.data`, `pre_chunk_part`)에 두었습니다. 이 버전으로 올리면 서버가 켜진 채로 백그라운드에서 하나씩 저장소 파일로 옮깁니다. 옮기기 전 것은 그대로 DB에서 받으므로 서비스는 멈추지 않습니다.

- **속도**: 하나 옮길 때마다 일한 시간의 `EBMS_STORE_MIGRATE_REST_RATIO`배(기본 3, 디스크 사용 약 25%)만큼 쉬고, 곡 임포트가 쓰기 잠금을 잡고 있으면 기다립니다. `EBMS_STORE_MIGRATE=0`이면 옮기지 않습니다.
- **순서와 공간 회수**: 사전 청크 → 차트 청크 → 곡 순서로 옮기고, 테이블 하나를 다 옮기면 그 BLOB 공간을 디스크에 돌려줍니다(`pre_chunk_part`·`song_part`는 `TRUNCATE`, `chart_chunk`·`song`은 BLOB을 비운 뒤라 빠르게 끝나는 `OPTIMIZE`). InnoDB는 행을 지워도 파일을 줄이지 않으므로 그 전까지는 공간이 돌아오지 않습니다. 곡 수십 개마다 `OPTIMIZE`하는 것은 남은 테이블 전체를 매번 다시 쓰고 복사본 공간도 필요해서 하지 않습니다.
- **필요한 여유 공간**: 가장 큰 테이블(보통 `song_part`) 크기 + `EBMS_STORE_MIN_FREE`(기본 20G). 모자라면 옮기기를 멈추고 10분마다 다시 확인합니다(로그 `migrate_to_store: Paused`). 크기는 다음으로 확인합니다.

  ```sql
  SELECT table_name, ROUND((data_length + index_length) / 1024 / 1024 / 1024, 1) AS gb
  FROM information_schema.tables WHERE table_schema = 'ebms' ORDER BY gb DESC;
  ```
- **진행도**: 로그의 `migrate_to_store(song): 1234/56789 (2%, ...)`. 도중에 꺼져도 다음 시작 때 남은 것부터 이어서 합니다.
- **정리**: 아무 행도 가리키지 않는 저장소 파일(지운 곡, 바뀌기 전 청크, 커밋하지 못한 파일)은 `EBMS_STORE_GC_HOURS`(기본 1)마다 지웁니다.

### 청크 크기 변경

차트 청크(`CHARTS_PER_CHUNK`, 100개)나 사전 청크(`SONGS_PER_PRE_CHUNK`, 8곡) 크기가 바뀐 버전으로 올리면, 서버가 시작한 뒤 백그라운드에서 기존 청크를 다시 묶습니다. 클라이언트는 바뀐 청크를 한 번 모두 다시 받습니다.

- 차트 청크: 기존 마지막 번호 뒤에 새 크기로 다시 쓴 뒤 옛 청크를 지웁니다. 그동안 클라이언트는 차트를 중복으로 볼 수는 있어도 빠뜨리지는 않습니다. 작업 중에는 곡 임포트가 기다리며, 도중에 꺼지면 다음 시작 때 이어서 합니다(`setting`의 `chart_rechunk_*`).
- 사전 청크: 기존 청크를 지우고 새 크기로 다시 만듭니다. 다 만들기 전에는 클라이언트가 없는 청크 대신 파일 단위로 받습니다.
