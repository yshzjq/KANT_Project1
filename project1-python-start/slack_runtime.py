"""봇과 main 사이의 상태 공유: SQLite 트랜잭션과 OS 파일 잠금."""

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


class FileLock:
    """프로세스 종료 시 OS가 해제하는 잠금. 오래된 PID 파일에 의존하지 않습니다."""

    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise AlreadyRunning(f"다른 프로세스가 사용 중입니다: {self.path.name}") from None
        return self

    def __exit__(self, *_):
        self.file.close()
        self.file = None


def is_locked(path):
    try:
        with FileLock(path):
            return False
    except AlreadyRunning:
        return True


class BotState:
    """이벤트를 먼저 보관하고, JSON 저장 성공 후에만 처리 완료로 표시합니다."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    pid INTEGER DEFAULT 0, heartbeat REAL DEFAULT 0,
                    connected INTEGER DEFAULT 0, stop INTEGER DEFAULT 0,
                    recovery INTEGER DEFAULT 0, recovered INTEGER DEFAULT -1,
                    processed INTEGER DEFAULT 0, requested INTEGER DEFAULT 0,
                    completed INTEGER DEFAULT 0, error TEXT DEFAULT ''
                );
                INSERT OR IGNORE INTO state (id) VALUES (1);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
                    received REAL NOT NULL
                );
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def execute(self, sql, params=()):
        with closing(self.connect()) as db, db:
            db.execute(sql, params)

    def snapshot(self):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN")
            state = dict(db.execute("SELECT * FROM state WHERE id=1").fetchone())
            rows = db.execute(
                "SELECT seq, payload FROM events WHERE seq > ? ORDER BY seq",
                (state["processed"],),
            ).fetchall()
        state["events"] = [json.loads(row["payload"]) for row in rows]
        state["last_event"] = rows[-1]["seq"] if rows else state["processed"]
        return state

    def begin(self):
        self.execute(
            "UPDATE state SET pid=?, heartbeat=?, connected=0, stop=0, "
            "recovery=recovery+1, error='' WHERE id=1", (os.getpid(), time.time()),
        )

    def heartbeat(self):
        self.execute("UPDATE state SET heartbeat=? WHERE id=1", (time.time(),))

    def connected(self):
        # 새 연결의 hello마다 끊겼던 동안의 변경을 다시 확인합니다.
        self.execute("UPDATE state SET connected=1, recovery=recovery+1 WHERE id=1")

    def disconnected(self):
        self.execute(
            "UPDATE state SET connected=0, recovery=recovery+1 WHERE id=1 AND connected=1"
        )

    def request(self, full=False):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE state SET requested=requested+1, recovery=recovery+? WHERE id=1",
                (int(full),),
            )
            return db.execute("SELECT requested FROM state WHERE id=1").fetchone()[0]

    def enqueue(self, event_id, event):
        self.execute(
            "INSERT OR IGNORE INTO events(event_id, payload, received) VALUES (?, ?, ?)",
            (event_id, json.dumps(event, ensure_ascii=False), time.time()),
        )

    def finish(self, snapshot):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE state SET recovered=?, processed=?, completed=?, error='' WHERE id=1",
                (snapshot["recovery"], snapshot["last_event"], snapshot["requested"]),
            )
            # 일주일 안의 재전송은 무시합니다. 미처리 이벤트는 지우지 않습니다.
            db.execute(
                "DELETE FROM events WHERE seq <= ? AND received < ?",
                (snapshot["last_event"], time.time() - 7 * 24 * 3600),
            )

    def fail(self, message):
        self.execute("UPDATE state SET error=? WHERE id=1", (message,))


def is_ready(state, request_id):
    return (
        state["connected"] and not state["stop"] and not state["error"]
        and 0 <= time.time() - state["heartbeat"] < 15
        and state["completed"] >= request_id
        and state["recovery"] == state["recovered"]
        and not state["events"]
    )
