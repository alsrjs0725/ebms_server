# EBMS Server

## Docker (서버 + MySQL 올인원)

```bash
cp .env.example .env   # 비밀번호 등 수정
docker compose up -d --build
```

- 서버: http://localhost:8000 (`EBMS_PORT`로 변경)
- MySQL 데이터는 `mysql-data`, 서버 런타임 파일(`var/`)은 `ebms-var` 볼륨에 유지됩니다.
- `docker/mysql/init/*.sql`은 MySQL 볼륨이 비어 있을 때 최초 1회만 실행됩니다.
  스키마를 바꾼 뒤 다시 적용하려면 `docker compose down -v`로 볼륨을 지우세요.
