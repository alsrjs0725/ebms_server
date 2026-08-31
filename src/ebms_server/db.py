import sqlite3
import os
import pathlib
import hashlib
import logging
import shutil
import uuid
import zipfile
import threading

from . import constant

class Database:
    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        
        self.logger = logging.getLogger(__name__)
        self._initialized = True
        if not constant.DB_PATH.exists():
            self.generate_database()
        self.chart_chunk_no = max(0, len(os.listdir(constant.CHART_DATA_DIR)) - 1)
        self.chart_chunk_sha256 = dict()
        for i in range(len(os.listdir(constant.CHART_DATA_DIR))):
            with open(self.get_chunk_file_path(i), "rb") as fos:
                self.chart_chunk_sha256[i] = hashlib.sha256(fos.read()).hexdigest()
    
    def get_chunk_file_path(self, number:int | None = None) -> os.PathLike:
        """mutable한 chart chunk file의 path를 가져오는 함수입니다
        
        Args:
            number: chunk의 숫자를 기입합니다. None(기본값)일시 mutable한 chunk의 path가 반환됩니다.

        Returns:
            os.PathLike
        """
        cur_path = constant.CHART_DATA_DIR / constant.CHART_CHUNK_FILENAME_TEMPLATE.format(self.chart_chunk_no)
        if (os.path.exists(cur_path) and os.path.getsize(cur_path) > constant.BYTE_PER_CHUNK):
            self.chart_chunk_no += 1
        return constant.CHART_DATA_DIR / constant.CHART_CHUNK_FILENAME_TEMPLATE.format(self.chart_chunk_no if (number is None) else number)

    def generate_database(self) -> None:
        """DB파일을 constant.py에 정의된 경로에 생성하는 함수입니다.

        """
        if (constant.DB_PATH.exists()):
            self.logger.error(f"DB 파일이 이미 존재합니다.")
            raise RuntimeError("DB 파일이 이미 존재합니다.")
        
        coms = [
            """
                CREATE TABLE song(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL
                );
            """,
            """
                CREATE TABLE chart(
                    id TEXT,
                    song_id INTEGER REFERENCES song(id),
                    size INTEGER,

                    PRIMARY KEY (id, size)
                );
            """
        ]

        con = sqlite3.connect(constant.DB_PATH)
        cur = con.cursor()

        for com in coms:
            cur.execute(com)
        con.commit()
        con.close()

    def insert_song(self, song_path:os.PathLike, remove=False) -> None:
        """BMS 노래 한 곡을 DB에 추가할 수 있는 함수입니다

        Args:
            song_path (os.PathLike): bms 파일을 포함한 에셋들이 담겨있는 폴더의 경로
        """

        root = pathlib.Path(song_path)
        if (not os.path.exists(root)):
            self.logger.warning(f"insert_song failed: Path doesn't exist[{str(root)}]")
            return
        if (not os.path.isdir(root)):
            self.logger.warning(f"insert_song failed: Path isn't directory")
            return
        for file_name in os.listdir(song_path):
            full_path = root / file_name
            if (full_path.suffix.lower() in constant.BMS_FORMAT): break;
        else:
            self.logger.warning(f"insert_song failed: No valid file in folder. Suporting ext: {constant.BMS_FORMAT}")
            return

        bms_files = []
        song_id = None
        lock = threading.Lock()
        
        with lock:
            con = sqlite3.connect(constant.DB_PATH)
            cur = con.cursor()
            for file in os.listdir(root):
                file_path = root / file
                if (file_path.suffix.lower() not in constant.BMS_FORMAT): continue
                
                with open(file_path, "rb") as fos:
                    sha256 = hashlib.sha256(fos.read()).hexdigest()
                size = os.path.getsize(file_path)

                cur.execute(
                    """
                    SELECT song_id
                    FROM chart
                    WHERE id = ? AND size = ?
                    """,
                    (sha256, size)
                )

                row = cur.fetchone()
                if (row is not None):
                    song_id = row[0]
                    
                bms_files.append((file_path, size, sha256))

            if (song_id is None):
                song_file_base_path = constant.SONG_DATA_DIR / f"{uuid.uuid4().hex}.zip"
                song_file_path = pathlib.Path(
                    self.create_zip(
                        root,
                        str(song_file_base_path),
                    )
                )

                com = """
                    INSERT INTO song (path) VALUES (?)
                """

                cur.execute(com, (str(song_file_path),))
                song_id = cur.lastrowid
                con.commit()
                
                self.logger.info(f"insert_song: Inserted new song[{str(song_file_path)}, {song_id}]")
                
            com = """
                INSERT INTO chart (id, song_id, size) 
                VALUES (?, ?, ?)
                ON CONFLICT(id, size) DO NOTHING
            """
            cnt = 0
            for (chart_file_path, size, sha256) in bms_files:
                
                cur.execute(com, (str(sha256), song_id, size))
                
                if cur.rowcount == 1:
                    with zipfile.ZipFile(self.get_chunk_file_path(), mode="a", compression=zipfile.ZIP_STORED) as zf:
                        zf.write(chart_file_path)
                    cnt += 1
            self.logger.info(f"insert_song: Inserted {cnt} charts.")
            con.commit()
            con.close()
            
        if remove:
            shutil.rmtree(song_path)

    def insert_songs(self, directory:os.PathLike, reculsive=False, remove=False) -> None:
        root_dir = pathlib.Path(directory)
        for file_name in os.listdir(root_dir):
            cur_dir = root_dir / file_name
            if (cur_dir).is_dir() and reculsive:
                self.insert_songs(cur_dir, True, remove)
                continue
            if cur_dir.suffix.lower() in constant.BMS_FORMAT:
                self.insert_song(root_dir)
                if remove:
                    shutil.rmtree(root_dir)
                break

    def create_zip(
        self,
        source_dir: os.PathLike,
        output_zip: os.PathLike,
    ) -> pathlib.Path:
        """
        디렉터리를 ZIP 파일로 압축하는 함수
        """

        source_dir = pathlib.Path(source_dir).resolve()
        output_zip = pathlib.Path(output_zip).resolve()

        if not source_dir.exists():
            raise FileNotFoundError(
                f"Source directory does not exist: {source_dir}"
            )

        if not source_dir.is_dir():
            raise NotADirectoryError(
                f"Source path is not a directory: {source_dir}"
            )

        output_zip.parent.mkdir(parents=True, exist_ok=True)

        def get_arcname(path: pathlib.Path) -> str:
            return path.relative_to(source_dir.parent).as_posix()

        def make_zipinfo(
            name: str,
            is_dir: bool = False,
        ) -> zipfile.ZipInfo:
            if is_dir and not name.endswith("/"):
                name += "/"

            info = zipfile.ZipInfo(name)

            # Unix permission 정보를 사용하지 않도록 설정
            info.create_system = 0
            info.external_attr = 0

            return info

        with zipfile.ZipFile(
            output_zip,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as zf:

            for path in source_dir.rglob("*"):

                # output_zip이 source_dir 안에 있을 경우 자기 자신 제외
                if path.resolve() == output_zip:
                    continue

                arcname = get_arcname(path)

                if path.is_dir():
                    # 빈 디렉터리는 명시적으로 저장
                    try:
                        next(path.iterdir())
                    except StopIteration:
                        info = make_zipinfo(
                            arcname,
                            is_dir=True,
                        )
                        zf.writestr(info, b"")

                elif path.is_file():
                    info = make_zipinfo(arcname)
                    info.compress_type = zipfile.ZIP_DEFLATED

                    with path.open("rb") as f:
                        zf.writestr(
                            info,
                            f.read(),
                            compress_type=zipfile.ZIP_DEFLATED,
                            compresslevel=6,
                        )

        return output_zip
    
    