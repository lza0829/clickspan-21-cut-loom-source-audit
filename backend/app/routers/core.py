from fastapi import APIRouter, HTTPException

from app.db import connect
from app.modules.idle_timeout_cut import list_cut_edges
from app.modules.loom_source_audit import resolve_source, verify
from app.seed import idle_timeout_sec
from app.services import ingest as ingest_svc
from app.services import sessions as sess_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        source = resolve_source(conn)
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
            # 织机顶栏「当前串珠源」：取切后承接会话，不再取最早会话。
            "loom_source_session_id": source["session_id"],
            "loom_source_visitor_id": source["visitor_id"],
            "loom_source_cut_edge_id": source["cut_edge_id"],
            "loom_source_from_audit": source["from_audit"],
            "loom_source_audit_rows": conn.execute(
                "SELECT COUNT(*) AS c FROM loom_source_audit"
            ).fetchone()["c"],
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
        if not detail["segments_aligned"]:
            raise HTTPException(
                409,
                {
                    "error": "珠串分段跨切边混段，拒绝返回成功",
                    "session_id": session_id,
                    "projection": sess_svc.segments_for_visitor(
                        conn, detail["session"]["visitor_id"]
                    ),
                },
            )
        return detail
    finally:
        conn.close()


@router.get("/strands")
def strands(visitor_id: str | None = None):
    """珠串分段投影只读接口：一段会话一段珠，跨切边不混段。"""
    conn = connect()
    try:
        if visitor_id:
            projection = sess_svc.segments_for_visitor(conn, visitor_id)
            if not projection["aligned"]:
                raise HTTPException(
                    409,
                    {
                        "error": "珠串分段跨切边混段，拒绝返回成功",
                        "projection": projection,
                    },
                )
            return projection
        projections = sess_svc.strand_projection(conn, None)
        return {
            "strand_order": sess_svc.STRAND_ORDER,
            "aligned": all(p["aligned"] for p in projections),
            "visitors": projections,
        }
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


@router.get("/loom-source-audit")
def loom_source_audit(
    visitor_id: str | None = None, cut_edge_id: int | None = None
):
    """织机源审计核证只读接口（基线所无）。

    逐行对撞：源审计 × 切边 × 会话成员物化（含两侧成员哈希、切分时刻核证、
    触发击点归属）。切边缺审计、审计与切边分叉、新源等于切前会话都以
    ``ok = False`` 与 ``divergences`` 报出，不给假成功。
    """
    conn = connect()
    try:
        return verify(conn, visitor_id=visitor_id, cut_edge_id=cut_edge_id)
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
