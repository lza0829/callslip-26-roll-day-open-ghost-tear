from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

SCHEMA = '''
CREATE TABLE IF NOT EXISTS business_kinds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL UNIQUE,
  seq INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  kind_ids TEXT NOT NULL,
  open INTEGER NOT NULL DEFAULT 1,
  current_ticket_id INTEGER
);
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL,
  kind_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  window_id INTEGER,
  queue_pos INTEGER NOT NULL,
  taken_at TEXT NOT NULL,
  called_at TEXT,
  finished_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  UNIQUE(business_day, slip_no),
  FOREIGN KEY(kind_id) REFERENCES business_kinds(id),
  FOREIGN KEY(window_id) REFERENCES windows(id)
);
CREATE TABLE IF NOT EXISTS call_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  window_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(ticket_id) REFERENCES tickets(id),
  FOREIGN KEY(window_id) REFERENCES windows(id)
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS day_rolls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_day TEXT NOT NULL UNIQUE,
  to_day TEXT NOT NULL,
  rolled_at TEXT NOT NULL,
  voided_count INTEGER NOT NULL DEFAULT 0,
  cleared_window_ids TEXT NOT NULL DEFAULT '',
  receipt TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS day_roll_voids (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  roll_id INTEGER NOT NULL,
  ticket_id INTEGER NOT NULL,
  slip_no TEXT NOT NULL,
  kind_id INTEGER NOT NULL,
  window_id INTEGER,
  prev_status TEXT NOT NULL,
  voided_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(roll_id) REFERENCES day_rolls(id)
);
CREATE INDEX IF NOT EXISTS idx_tickets_day_status ON tickets(business_day, status);
CREATE INDEX IF NOT EXISTS idx_day_roll_voids_roll ON day_roll_voids(roll_id);
'''


def connect() -> sqlite3.Connection:
    # isolation_level=None: 由 db() 显式控制事务边界，避免隐式事务与 BEGIN IMMEDIATE 冲突
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def db(readonly: bool = False):
    """事务边界：写路径 BEGIN IMMEDIATE 串行化，读路径共用一致性快照。

    切换营业日与办结/叫号/过号等写操作因此不会交错成双终态；
    任一步异常整笔回滚，不留半截。
    """
    conn = connect()
    try:
        conn.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db() -> None:
    # executescript 自带隐式事务，不走 db() 的显式 BEGIN/COMMIT
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate_slip_unique(conn)
    finally:
        conn.close()


def _migrate_slip_unique(conn: sqlite3.Connection) -> None:
    """旧库的 tickets.slip_no 为全局 UNIQUE，会阻断新营业日号段归零后重发同号。

    改为 UNIQUE(business_day, slip_no)：同一营业日内号牌唯一，跨日可重发。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tickets'"
    ).fetchone()
    if not row or "slip_no TEXT NOT NULL UNIQUE" not in (row["sql"] or ""):
        return
    conn.executescript(
        """
        PRAGMA foreign_keys=OFF;
        BEGIN IMMEDIATE;
        DROP INDEX IF EXISTS idx_tickets_day_status;
        ALTER TABLE tickets RENAME TO tickets_old;
        CREATE TABLE tickets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slip_no TEXT NOT NULL,
          kind_id INTEGER NOT NULL,
          business_day TEXT NOT NULL,
          status TEXT NOT NULL,
          window_id INTEGER,
          queue_pos INTEGER NOT NULL,
          taken_at TEXT NOT NULL,
          called_at TEXT,
          finished_at TEXT,
          note TEXT NOT NULL DEFAULT '',
          UNIQUE(business_day, slip_no),
          FOREIGN KEY(kind_id) REFERENCES business_kinds(id),
          FOREIGN KEY(window_id) REFERENCES windows(id)
        );
        INSERT INTO tickets(id, slip_no, kind_id, business_day, status, window_id,
                            queue_pos, taken_at, called_at, finished_at, note)
          SELECT id, slip_no, kind_id, business_day, status, window_id,
                 queue_pos, taken_at, called_at, finished_at, note
          FROM tickets_old;
        DROP TABLE tickets_old;
        CREATE INDEX IF NOT EXISTS idx_tickets_day_status ON tickets(business_day, status);
        COMMIT;
        PRAGMA foreign_keys=ON;
        """
    )
