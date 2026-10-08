# 배포·운영

## 포트와 데이터

| 포트 | 서비스 | 외부 공개 | 비고 |
| --- | --- | --- | --- |
| 8000/tcp (`EBMS_PORT`) | EBMS 서버 (HTTP) | 필요 | 방화벽/리버스 프록시에서 열어야 하는 유일한 포트 |
| 3306/tcp | MySQL | 불필요 | 호스트에 바인딩하지 않음. compose 내부 네트워크에서 `ebms` 컨테이너만 접근 |

- 곡/차트 데이터는 MySQL(`mysql-data` 볼륨)에 BLOB으로 저장되며, 테이블은 서버가 시작할 때 자동 생성됩니다.
- 서버 로그와 임포트 대기 폴더(`var/log`, `var/tmp`)는 `ebms-var` 볼륨에 유지됩니다.

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

   등록 정보는 `runner` 볼륨에 남으므로 이후 재시작에는 토큰이 필요 없습니다. 다시 등록하려면 `docker compose down -v` 후 새 토큰으로 올립니다. 다른 저장소용 runner가 필요하면 `RUNNER_URL`, `RUNNER_NAME`, `RUNNER_LABELS`를 바꿔 다른 폴더(compose 프로젝트)로 띄웁니다.

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

> `var/tmp/` 임포트는 테스트용 임시 업로드 경로입니다. 추후 관리자 페이지를 통한 업로드 방식이 추가될 예정입니다.

곡 등록용 HTTP API는 아직 없습니다. 현재는 서버 시작 시 `var/tmp/` 아래 폴더를 훑어 차트 파일이 있는 폴더를 곡으로 등록하고, 등록한 폴더는 삭제합니다. 같은 차트(SHA-256과 크기 동일)가 이미 있으면 기존 곡에 연결됩니다.

## 이전 버전 데이터 마이그레이션

서버가 시작할 때 없는 컬럼을 추가하고 기존 곡의 매니페스트를 채웁니다. 차트 청크의 항목 이름 변경은 한 번 직접 실행합니다 (청크 해시가 바뀌어 클라이언트가 다시 받습니다).

```bash
docker compose exec ebms python -m ebms_server.migrate
```
