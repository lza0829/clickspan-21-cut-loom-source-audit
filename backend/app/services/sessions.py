"""会话查询与珠串分段投影。

珠串分段投影（只读）：``session_detail`` 只回该会话自己的击点，绝不按访客
拉全量击点再由页面自行连成一段——切边两侧的击点因此不会混在同一段珠串里。
投影同时给出 ``strands``（按会话分段的珠串序列）与 ````loom_source``（顶栏
当前源）两份视图，均与 ``idle_cut_edges`` / ``loom_source_audit`` 同源。

分段一致性：:func:`segments_for_visitor` 在返回前核对「切边相邻」与「击点集
互斥且并集为访客全量」，任何一段越界混入邻段击点即报错，读路径不会把跨切边
的混段当成功返回。
"""

import json

from app.modules.loom_source_audit import (
    member_digest,
    ordered_member_ids,
    resolve_source,
)

STRAND_ORDER = "session_scoped"


def _hits_for_session(conn, session_id: str) -> list:
    rows = conn.execute(
        """
        SELECT id, visitor_id, session_id, event_ts, page_path, event_name, channel
        FROM hits
        WHERE session_id=? AND channel='primary'
        ORDER BY event_ts, id
        """,
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _latest_proof(conn, session_id: str):
    return conn.execute(
        "SELECT * FROM session_proofs WHERE session_id=? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()


def _strand(conn, sess_row) -> dict:
    hits = _hits_for_session(conn, sess_row["id"])
    hit_ids = [h["id"] for h in hits]
    proof = _latest_proof(conn, sess_row["id"])
    proof_ids = None
    if proof is not None:
        try:
            proof_ids = list(json.loads(proof["hit_ids_json"]))
        except (TypeError, ValueError):
            proof_ids = None
    strand = {
        "session_id": sess_row["id"],
        "visitor_id": sess_row["visitor_id"],
        "status": sess_row["status"],
        "started_at": sess_row["started_at"],
        "ended_at": sess_row["ended_at"],
        "hit_count": len(hits),
        "declared_hit_count": sess_row["hit_count"],
        "member_ids": hit_ids,
        "members_hash": member_digest(hit_ids),
        "hits": hits,
        "proof_member_ids": proof_ids,
        "proof_aligned": proof_ids == hit_ids,
    }
    return strand


def segments_for_visitor(conn, visitor_id: str) -> dict:
    """珠串按会话分段：一段一会话，跨切边不混段。"""
    sessions = conn.execute(
        """
        SELECT * FROM sessions WHERE visitor_id=?
        ORDER BY started_at, id
        """,
        (visitor_id,),
    ).fetchall()
    strands = [_strand(conn, s) for s in sessions]

    edges = conn.execute(
        """
        SELECT * FROM idle_cut_edges WHERE visitor_id=? ORDER BY id
        """,
        (visitor_id,),
    ).fetchall()

    # 切边必须恰好连接相邻两段：旧段在前，新段紧随其后。
    position = {s["session_id"]: i for i, s in enumerate(strands)}
    ordered_pairs = []
    for i in range(1, len(strands)):
        ordered_pairs.append((strands[i - 1]["session_id"], strands[i]["session_id"]))
    edge_pairs = [(e["old_session_id"], e["new_session_id"]) for e in edges]

    seen = set()
    duplicated = []
    for s in strands:
        for hid in s["member_ids"]:
            if hid in seen:
                duplicated.append(hid)
            seen.add(hid)
    all_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM hits WHERE visitor_id=? AND channel='primary'",
            (visitor_id,),
        ).fetchall()
    ]
    missing = sorted(set(all_ids) - seen)

    aligned = (
        not duplicated
        and not missing
        and sorted(edge_pairs) == sorted(ordered_pairs)
        and all(s["proof_aligned"] for s in strands)
        and all(
            (position[e["old_session_id"]] + 1 == position[e["new_session_id"]])
            for e in edges
            if e["old_session_id"] in position and e["new_session_id"] in position
        )
    )

    return {
        "visitor_id": visitor_id,
        "strand_order": STRAND_ORDER,
        "aligned": aligned,
        "segments": strands,
        "strands": strands,
        "cut_edges": [dict(e) for e in edges],
        "crossing_member_ids": duplicated,
        "missing_member_ids": missing,
        "members_total": len(all_ids),
    }


def list_sessions(conn, visitor_id: str | None = None):
    if visitor_id:
        rows = conn.execute(
            """
            SELECT id, visitor_id, started_at, ended_at, hit_count, status
            FROM sessions WHERE visitor_id=?
            ORDER BY started_at
            """,
            (visitor_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, visitor_id, started_at, ended_at, hit_count, status
            FROM sessions ORDER BY started_at
            """
        ).fetchall()
    return [dict(r) for r in rows]


def session_detail(conn, session_id: str):
    sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        return None
    # 珠串按会话拉点：只回该会话自己的成员，切边另一侧的击点不入本段。
    hits = _hits_for_session(conn, session_id)
    proof = _latest_proof(conn, session_id)
    cut_in = conn.execute(
        "SELECT * FROM idle_cut_edges WHERE new_session_id=?",
        (session_id,),
    ).fetchall()
    cut_out = conn.execute(
        "SELECT * FROM idle_cut_edges WHERE old_session_id=?",
        (session_id,),
    ).fetchall()
    audit = conn.execute(
        """
        SELECT * FROM loom_source_audit
        WHERE old_session_id=? OR new_session_id=?
        ORDER BY id
        """,
        (session_id, session_id),
    ).fetchall()
    source = resolve_source(conn)
    projection = segments_for_visitor(conn, sess["visitor_id"])
    hit_ids = [h["id"] for h in hits]
    return {
        "session": dict(sess),
        "hits": hits,
        "member_ids": hit_ids,
        "members_hash": member_digest(hit_ids),
        "proof": dict(proof) if proof else None,
        "cut_edges": [dict(r) for r in (*cut_out, *cut_in)],
        "source_audit": [dict(r) for r in audit],
        "loom_source_session_id": source["session_id"],
        "loom_source": source,
        "segment_count": len(projection["segments"]),
        "segments_aligned": projection["aligned"],
    }


def strand_projection(conn, visitor_id: str | None = None):
    """珠串分段投影只读接口后端：按会话分段返回，不跨切边混段。"""
    if visitor_id:
        return [segments_for_visitor(conn, visitor_id)]
    visitor_ids = [
        r["visitor_id"]
        for r in conn.execute("SELECT DISTINCT visitor_id FROM sessions ORDER BY visitor_id")
    ]
    return [segments_for_visitor(conn, vid) for vid in visitor_ids]
