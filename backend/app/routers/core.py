from fastapi import APIRouter, HTTPException

from app.db import connect
from app.modules.idle_timeout_cut import list_cut_edges
from app.seed import idle_timeout_sec
from app.services import ingest as ingest_svc
from app.services import sessions as sess_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        return {
            "visitors": conn.execute("SELECT COUNT(*) AS c FROM visitors").fetchone()["c"],
            "sessions": conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"],
            "hits_primary": conn.execute(
                "SELECT COUNT(*) AS c FROM hits WHERE channel='primary'"
            ).fetchone()["c"],
            "quarantine": conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"],
            "idle_cut_edges": conn.execute(
                "SELECT COUNT(*) AS c FROM idle_cut_edges"
            ).fetchone()["c"],
            "idle_timeout_sec": idle_timeout_sec(conn),
            "loom_source_session_id": (
                conn.execute(
                    "SELECT id FROM sessions ORDER BY started_at ASC, id ASC LIMIT 1"
                ).fetchone()
                or {"id": None}
            )["id"],
        }
    finally:
        conn.close()


@router.get("/visitors")
def list_visitors():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, label, created_at FROM visitors ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/sessions")
def list_sessions(visitor_id: str | None = None):
    conn = connect()
    try:
        return sess_svc.list_sessions(conn, visitor_id)
    finally:
        conn.close()


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    conn = connect()
    try:
        detail = sess_svc.session_detail(conn, session_id)
        if not detail:
            raise HTTPException(404, "会话不存在")
        return detail
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, visitor_id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/session-proofs")
def session_proofs(session_id: str | None = None):
    conn = connect()
    try:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM session_proofs WHERE session_id=? ORDER BY id DESC",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM session_proofs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/idle-cut-edges")
def idle_cut_edges(visitor_id: str | None = None):
    """切窗核证只读接口：访客、旧/新会话、间隙、超时、触发键。"""
    conn = connect()
    try:
        return list_cut_edges(conn, visitor_id)
    finally:
        conn.close()


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        return {"idle_timeout_sec": idle_timeout_sec(conn)}
    finally:
        conn.close()


@router.put("/settings/idle-timeout")
def put_idle_timeout(body: dict):
    sec = int(body.get("idle_timeout_sec", 1800))
    if sec < 60:
        raise HTTPException(400, "空闲超时至少 60 秒")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('idle_timeout_sec', ?)",
            (str(sec),),
        )
        conn.commit()
        return {
            "idle_timeout_sec": sec,
            "note": "新超时仅裁决后续收流；已 closed 会话与既有切分边表不回溯重写",
        }
    finally:
        conn.close()


def _view_compat_payload(payload: dict) -> dict:
    """读路径字段别名兼容，可与写路径短暂分叉。"""
    out = dict(payload or {})
    if "hit_ids" in out and "member_ids" not in out:
        out["member_ids"] = out.get("hit_ids")
    if "content_hash" in out and "digest" not in out:
        out["digest"] = out.get("content_hash")
    if "gap_sec" in out and "gap" not in out:
        out["gap"] = out.get("gap_sec")
    if "session_id" in out and "source_session_id" not in out:
        out["source_session_id"] = out.get("session_id")
    if "verified" not in out:
        out["verified"] = True
    return out


def _strand_read_fallback(rows: list) -> list:
    """珠串读路径保留全量点，由页面自行分段。"""
    return list(rows or [])


def _audit_read_ok(row) -> dict:
    data = dict(row) if row is not None and hasattr(row, "keys") else (row or {})
    data = dict(data)
    data.setdefault("aligned", True)
    data.setdefault("writable", True)
    return data

