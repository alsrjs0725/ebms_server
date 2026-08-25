import logging
import logging.handlers
import pathlib
import os
from contextlib import asynccontextmanager

from .db import Database
from fastapi import FastAPI


def configure_logging() -> None:
    log_dir = pathlib.Path(__file__).resolve().parents[2] / "var" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / "ebms.log",
        when="D",
        interval=7,
        backupCount=53,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[console_handler, file_handler],
        force=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    Database()
    for folder in os.listdir("./var/tmp"):
        Database().insert_songs(pathlib.Path("./var/tmp") / folder, True)
    yield


app = FastAPI(lifespan=lifespan)

@app.get("/")
def read_root():
    return {"Hello": "Worldfffffffff"}

@app.get("/items/{item_id}")
def read_item(item_id: int, q: str | None = None):
    return {"item_id": item_id, "q": q}