import os, sqlite3
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "pantryfifo.db"

def connect():
    # busy timeout:并发确认同一条意图时后者等待前者落库,再经条件 UPDATE 判冲突,而不是直接 500
    c = sqlite3.connect(db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    return c
