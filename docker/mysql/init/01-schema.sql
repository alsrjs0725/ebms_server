-- 최초 기동 시(빈 볼륨) 한 번만 실행됩니다.
-- 현재 SQLite 스키마(song, chart)를 그대로 옮긴 초기 버전입니다.
-- MySQL 마이그레이션/BLOB 작업에서 최종 스키마로 교체될 예정입니다.

CREATE TABLE IF NOT EXISTS song (
    id   INT NOT NULL AUTO_INCREMENT,
    path VARCHAR(255) NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chart (
    id      CHAR(64)        NOT NULL,
    song_id INT             NULL,
    size    BIGINT          NOT NULL,
    PRIMARY KEY (id, size),
    KEY idx_chart_song_id (song_id),
    CONSTRAINT fk_chart_song FOREIGN KEY (song_id) REFERENCES song (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
