import json

from app.db import FIXTURES_DIR, connect
from app.modules.idle_timeout_cut import place_hit

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visitors (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  hit_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS hits (
  id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  session_id TEXT REFERENCES sessions(id),
  event_ts TEXT NOT NULL,
  page_path TEXT NOT NULL,
  event_name TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT 'primary',
  UNIQUE(visitor_id, event_ts, page_path, event_name)
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visitor_id TEXT,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS session_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  visitor_id TEXT NOT NULL,
  hit_ids_json TEXT NOT NULL,
  hit_count INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idle_cut_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visitor_id TEXT NOT NULL,
  old_session_id TEXT NOT NULL,
  new_session_id TEXT NOT NULL,
  gap_sec INTEGER NOT NULL,
  idle_timeout_sec INTEGER NOT NULL,
  trigger_hit_id TEXT NOT NULL,
  trigger_ts TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT 'idle_timeout',
  created_at TEXT NOT NULL,
  UNIQUE(old_session_id, new_session_id)
);

CREATE TABLE IF NOT EXISTS loom_source_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visitor_id TEXT NOT NULL,
  cut_edge_id INTEGER NOT NULL UNIQUE REFERENCES idle_cut_edges(id),
  old_session_id TEXT NOT NULL,
  new_session_id TEXT NOT NULL,
  trigger_hit_id TEXT NOT NULL,
  old_members_hash TEXT NOT NULL,
  new_members_hash TEXT NOT NULL,
  old_member_count INTEGER NOT NULL,
  new_member_count INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  CHECK (old_session_id <> new_session_id)
);
"""


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM settings WHERE key='idle_timeout_sec'").fetchone()
    if not row:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('idle_timeout_sec', ?)",
            ("1800",),
        )
    n = conn.execute("SELECT COUNT(*) AS c FROM visitors").fetchone()["c"]
    if n == 0:
        _seed_fixtures(conn)
    # 旧库迁移：既有切边若缺源审计，按成员物化与切分时刻核证补齐；
    # 新切分一律由切窗同事务写审计，不走这条。
    from app.modules.loom_source_audit import backfill_missing

    backfill_missing(conn)
    conn.commit()
    conn.close()


def _seed_fixtures(conn):
    fixture = FIXTURES_DIR / "seed_hits.json"
    if not fixture.exists():
        return
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for v in data.get("visitors", []):
        conn.execute(
            "INSERT INTO visitors(id, label, created_at) VALUES (?,?,?)",
            (v["id"], v["label"], v["created_at"]),
        )
    # 物化与收流同源：按 event_ts 顺序逐点走空闲超时裁决，
    # 超间隙即同事务关旧 / 开新 / 写切分边表与会话核证。
    timeout_sec = idle_timeout_sec(conn)
    hits = sorted(data.get("hits", []), key=lambda h: (h["event_ts"], h["id"]))
    for h in hits:
        place_hit(
            conn,
            {
                "id": h["id"],
                "visitor_id": h["visitor_id"],
                "event_ts": h["event_ts"],
                "page_path": h["page_path"],
                "event_name": h["event_name"],
            },
            timeout_sec=timeout_sec,
        )


def idle_timeout_sec(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key='idle_timeout_sec'").fetchone()
    return int(row["value"]) if row else 1800
