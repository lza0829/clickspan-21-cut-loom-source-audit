"""空闲超时切窗（功能题 01）。

裁决：同一访客的新击点与当前 open 会话末击点的间隙严格大于
``idle_timeout_sec`` 时，先关闭旧会话、再开新会话承接；未超则并入当前
open 会话。

持久化形态（择一之「独立切分边表」）：每次切分落一行
``idle_cut_edges``，记录旧/新会话、间隙秒、超时值与触发击点。

提交形态（择一之「收流同事务」）：本模块所有写操作只使用调用方传入的
连接、不自行开关事务；由收流在同一个 ``BEGIN IMMEDIATE`` 事务内完成
关旧 / 开新 / 改挂 / 会话核证 / 切分边表 / 织机源审计，任何一步失败整体
回滚，杜绝半截切分双挂或旧未关已开新。

切窗成功即同事务重挂源审计：``_cut_to_new_session`` 在关旧、开新、写切边、
两侧成员核证之后立刻调用 :mod:`app.modules.loom_source_audit`
:func:`record_cut_source`，写不出审计行就抛错回滚，不存在「切边已落而源审计
缺行」的成功切窗。
"""

import json
import uuid
from datetime import datetime, timezone

from app.modules.loom_source_audit import record_cut_source

STATUS = "implemented"

EDGE_REASON = "idle_timeout"


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    # 夹具与库内统一为 UTC 的 ISO8601（Z 结尾）。
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def gap_seconds(prev_ts: str, hit_ts: str) -> int:
    """新击点与末击点之间的空闲秒数（非负）。"""
    delta = (parse_ts(hit_ts) - parse_ts(prev_ts)).total_seconds()
    return int(delta)


def idle_timeout_sec(conn) -> int:
    row = conn.execute(
        "SELECT value FROM settings WHERE key='idle_timeout_sec'"
    ).fetchone()
    return int(row["value"]) if row else 1800


def get_open_session(conn, visitor_id: str):
    """同访客当前唯一 open 会话及其末击点时刻。"""
    return conn.execute(
        """
        SELECT s.*,
               (SELECT MAX(event_ts) FROM hits WHERE session_id = s.id) AS last_hit_ts
        FROM sessions s
        WHERE s.visitor_id = ? AND s.status = 'open'
        ORDER BY s.started_at DESC, s.id DESC
        LIMIT 1
        """,
        (visitor_id,),
    ).fetchone()


def _new_session_id(visitor_id: str) -> str:
    return f"S-{visitor_id}-{uuid.uuid4().hex[:6]}"


def _write_proof(conn, session_row) -> None:
    """会话物化核证：不可变成员 id 快照 + 起止 + 点数。"""
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts, id",
            (session_row["id"],),
        ).fetchall()
    ]
    conn.execute(
        """
        INSERT INTO session_proofs(
          session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            session_row["id"],
            session_row["visitor_id"],
            json.dumps(ids, ensure_ascii=False),
            session_row["hit_count"],
            session_row["started_at"],
            session_row["ended_at"],
            now_stamp(),
        ),
    )


def place_hit(conn, hit: dict, timeout_sec: int | None = None) -> dict:
    """在调用方事务内为单个击点裁决归属并落库。

    返回 ``{"action": "attach"|"cut"|"open"|"duplicate", ...}``；调用方
    不得在本函数之外再自行开关会话，切分与核证在同一失败闭内完成。
    """
    visitor_id = hit["visitor_id"]
    hit_ts = hit["event_ts"]
    if timeout_sec is None:
        timeout_sec = idle_timeout_sec(conn)

    # 幂等前置：主键或自然唯一键已存在则原样跳过，
    # 绝不为重复击点先关旧开新（否则产生没有击点的空切分）。
    existing = conn.execute(
        "SELECT session_id FROM hits WHERE id=? OR "
        "(visitor_id=? AND event_ts=? AND page_path=? AND event_name=?)",
        (hit["id"], visitor_id, hit_ts, hit["page_path"], hit["event_name"]),
    ).fetchone()
    if existing is not None:
        return {
            "action": "duplicate",
            "session_id": existing["session_id"],
            "gap_sec": None,
            "old_session_id": None,
            "new_session_id": None,
            "timeout_sec": timeout_sec,
        }

    open_sess = get_open_session(conn, visitor_id)
    action = "open"
    gap = None
    old_sid = None
    new_sid = None
    cut_edge_id = None

    if open_sess is not None and open_sess["last_hit_ts"] is not None:
        gap = gap_seconds(open_sess["last_hit_ts"], hit_ts)
        if gap <= timeout_sec:
            # 未超：并入当前 open 会话（迟到击点同样落此，不触发切分）。
            sid = open_sess["id"]
            action = "attach"
        else:
            # 超：同事务先关旧会话、再开新会话承接，写切分边表与双段核证。
            sid, cut_edge_id = _cut_to_new_session(
                conn, open_sess, hit, gap, timeout_sec
            )
            old_sid = open_sess["id"]
            new_sid = sid
            action = "cut"
    else:
        sid = _new_session_id(visitor_id)
        conn.execute(
            """
            INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)
            VALUES (?,?,?,?,0,'open')
            """,
            (sid, visitor_id, hit_ts, hit_ts),
        )
        new_sid = sid

    conn.execute(
        """
        INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)
        VALUES (?,?,?,?,?,?, 'primary')
        """,
        (
            hit["id"],
            visitor_id,
            sid,
            hit_ts,
            hit["page_path"],
            hit["event_name"],
        ),
    )

    conn.execute(
        """
        UPDATE sessions
        SET hit_count = hit_count + 1,
            ended_at = CASE
              WHEN ended_at IS NULL OR ended_at < ? THEN ?
              ELSE ended_at END
        WHERE id = ?
        """,
        (hit_ts, hit_ts, sid),
    )
    sess_row = conn.execute(
        "SELECT * FROM sessions WHERE id=?", (sid,)
    ).fetchone()
    _write_proof(conn, sess_row)

    if action == "cut":
        # 切窗成功必须同事务重挂源审计：写不出审计行即抛错，由收流的
        # BEGIN IMMEDIATE 整体回滚，绝不留下「切边已落而源审计缺行」。
        record_cut_source(
            conn,
            visitor_id=visitor_id,
            cut_edge_id=cut_edge_id,
            old_session_id=old_sid,
            new_session_id=sid,
            trigger_hit_id=hit["id"],
        )

    return {
        "action": action,
        "session_id": sid,
        "gap_sec": gap,
        "old_session_id": old_sid,
        "new_session_id": new_sid,
        "cut_edge_id": cut_edge_id,
        "timeout_sec": timeout_sec,
    }


def _cut_to_new_session(conn, open_sess, hit: dict, gap: int, timeout_sec: int):
    """同事务内关旧、开新、写切分边表；旧会话补一份封闭核证。

    返回 ``(new_session_id, cut_edge_id)``；源审计由 :func:`place_hit` 在
    触发击点落库与承接会话核证写完之后同事务补写。
    """
    visitor_id = open_sess["visitor_id"]
    old_sid = open_sess["id"]
    hit_ts = hit["event_ts"]

    # 1) 关旧：状态置 closed，结束时刻锁为末击点时刻。
    conn.execute(
        "UPDATE sessions SET status='closed', ended_at=? WHERE id=?",
        (open_sess["last_hit_ts"], old_sid),
    )
    closed_row = conn.execute(
        "SELECT * FROM sessions WHERE id=?", (old_sid,)
    ).fetchone()
    _write_proof(conn, closed_row)

    # 2) 开新：触发击点承接会话。
    new_sid = _new_session_id(visitor_id)
    conn.execute(
        """
        INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)
        VALUES (?,?,?,?,0,'open')
        """,
        (new_sid, visitor_id, hit_ts, hit_ts),
    )

    # 3) 持久切分边表（独立边表形态，禁止只在内存分组）。
    cur = conn.execute(
        """
        INSERT INTO idle_cut_edges(
          visitor_id, old_session_id, new_session_id,
          gap_sec, idle_timeout_sec, trigger_hit_id, trigger_ts, reason, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            visitor_id,
            old_sid,
            new_sid,
            gap,
            timeout_sec,
            hit["id"],
            hit_ts,
            EDGE_REASON,
            now_stamp(),
        ),
    )
    cut_edge_id = int(cur.lastrowid)
    if not cut_edge_id:
        raise RuntimeError("空闲切窗：切分边写入后无主键")
    return new_sid, cut_edge_id


def list_cut_edges(conn, visitor_id: str | None = None):
    """切窗核证只读查询：访客、旧/新会话、间隙、超时、触发键。"""
    if visitor_id:
        rows = conn.execute(
            "SELECT * FROM idle_cut_edges WHERE visitor_id=? ORDER BY id",
            (visitor_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM idle_cut_edges ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]
