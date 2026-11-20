"""织机源审计（切窗源审计 + 分段投影互证）。

持久化形态（择一之「独立织机源审计表」）：每次空闲超时切分在同一事务内往
``loom_source_audit`` 落一行，记录访客、切边键、旧源 / 新源会话、触发击点
以及两侧成员哈希（有序击点 id 的 sha256）。切边键唯一，一个切分对应且仅
对应一行源审计。

提交形态（择一之「切窗成功同事务重挂源审计并刷新分段投影，缺一回滚」）：
本模块所有写操作只使用调用方传入的连接，绝不自行 commit。``idle_timeout_cut``
在关旧 / 开新 / 写切边 / 会话成员物化之后调用 :func:`record_cut_source`；
任一步失败，收流的 ``BEGIN IMMEDIATE`` 整体回滚，因此不会出现「切窗成功而
源审计缺行」。分段投影不落冗余表，读路径按 ``sessions`` / ``hits`` /
``session_proofs`` / ``idle_cut_edges`` 实时投影，与源审计同源，不会落后
于切边。

核证：:func:`verify` 把源审计逐行对撞切边与会话成员物化（击点集、核证快照、
切边邻接关系），任何分叉都以 ``ok = False`` 报出；:func:`resolve_source`
给出织机顶栏的「当前串珠源」，只认与切边一致的审计行，审计被篡改时退回
切边的新承接会话，绝不把顶栏停在切前会话。
"""

import hashlib
import json
from datetime import datetime, timezone

STATUS = "implemented"

DIGEST_PREFIX = "sha256:"

RECORD_FORM = "same_transaction_remount"


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def member_digest(hit_ids) -> str:
    """有序击点 id 集的稳定哈希（两侧成员哈希的统一算法）。"""
    payload = json.dumps(list(hit_ids), ensure_ascii=False, separators=(",", ":"))
    return DIGEST_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ordered_member_ids(conn, session_id: str) -> list:
    return [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts, id",
            (session_id,),
        ).fetchall()
    ]


def _earliest_proof_ids(conn, session_id: str):
    """切分时刻新源成员快照：新会话最早一份核证即触发击点集。"""
    row = conn.execute(
        """
        SELECT hit_ids_json FROM session_proofs
        WHERE session_id=? ORDER BY id ASC LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        return list(json.loads(row["hit_ids_json"]))
    except (TypeError, ValueError):
        return None


def _latest_proof_ids(conn, session_id: str):
    row = conn.execute(
        """
        SELECT hit_ids_json FROM session_proofs
        WHERE session_id=? ORDER BY id DESC LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        return list(json.loads(row["hit_ids_json"]))
    except (TypeError, ValueError):
        return None


def _insert_row(
    conn,
    *,
    visitor_id: str,
    cut_edge_id: int,
    old_session_id: str,
    new_session_id: str,
    trigger_hit_id: str,
    old_ids: list,
    new_ids: list,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO loom_source_audit(
          visitor_id, cut_edge_id, old_session_id, new_session_id, trigger_hit_id,
          old_members_hash, new_members_hash, old_member_count, new_member_count,
          created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            visitor_id,
            cut_edge_id,
            old_session_id,
            new_session_id,
            trigger_hit_id,
            member_digest(old_ids),
            member_digest(new_ids),
            len(old_ids),
            len(new_ids),
            now_stamp(),
        ),
    )
    return int(cur.lastrowid)


def record_cut_source(
    conn,
    visitor_id: str,
    cut_edge_id: int,
    old_session_id: str,
    new_session_id: str,
    trigger_hit_id: str,
) -> dict:
    """切窗同事务重挂源审计；任何不满足都抛错，由调用方事务整体回滚。"""
    if old_session_id == new_session_id:
        raise RuntimeError("织机源审计：新源不得等于切前会话")
    old_ids = ordered_member_ids(conn, old_session_id)
    new_ids = ordered_member_ids(conn, new_session_id)
    if not old_ids:
        raise RuntimeError("织机源审计：切前会话无成员，拒绝落审计")
    if not new_ids:
        raise RuntimeError("织机源审计：承接会话无成员，拒绝落审计")
    if trigger_hit_id not in new_ids:
        raise RuntimeError("织机源审计：触发击点未落在新源会话")
    row_id = _insert_row(
        conn,
        visitor_id=visitor_id,
        cut_edge_id=cut_edge_id,
        old_session_id=old_session_id,
        new_session_id=new_session_id,
        trigger_hit_id=trigger_hit_id,
        old_ids=old_ids,
        new_ids=new_ids,
    )
    row = conn.execute(
        "SELECT * FROM loom_source_audit WHERE id=?", (row_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("织机源审计：写入后不可见")
    return dict(row)


def backfill_missing(conn) -> int:
    """为历史切边补源审计（仅用于迁移旧库；新切分一律走 record_cut_source）。"""
    rows = conn.execute(
        """
        SELECT e.* FROM idle_cut_edges e
        LEFT JOIN loom_source_audit a ON a.cut_edge_id = e.id
        WHERE a.id IS NULL
        ORDER BY e.id
        """
    ).fetchall()
    written = 0
    for e in rows:
        old_ids = ordered_member_ids(conn, e["old_session_id"])
        new_ids = _earliest_proof_ids(conn, e["new_session_id"])
        if new_ids is None:
            new_ids = [e["trigger_hit_id"]]
        if not old_ids or not new_ids:
            continue
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO loom_source_audit(
              visitor_id, cut_edge_id, old_session_id, new_session_id, trigger_hit_id,
              old_members_hash, new_members_hash, old_member_count, new_member_count,
              created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                e["visitor_id"],
                e["id"],
                e["old_session_id"],
                e["new_session_id"],
                e["trigger_hit_id"],
                member_digest(old_ids),
                member_digest(new_ids),
                len(old_ids),
                len(new_ids),
                now_stamp(),
            ),
        )
        if cur.rowcount:
            written += 1
    return written


def resolve_source(conn) -> dict:
    """织机顶栏「当前串珠源」：只认与切边同源的审计行。

    审计行被人为改成切前会话 / 删行时，JOIN 会把它排除，退回最近切边的
    新承接会话；因此顶栏永远不会停在切前 id，分叉由 :func:`verify` 报失败。
    """
    row = conn.execute(
        """
        SELECT a.new_session_id, a.visitor_id, a.cut_edge_id
        FROM loom_source_audit a
        JOIN idle_cut_edges e ON e.id = a.cut_edge_id
        WHERE e.visitor_id = a.visitor_id
          AND e.old_session_id = a.old_session_id
          AND e.new_session_id = a.new_session_id
          AND e.trigger_hit_id = a.trigger_hit_id
        ORDER BY a.id DESC
        LIMIT 1
        """
    ).fetchone()
    from_audit = row is not None
    if row is None:
        row = conn.execute(
            """
            SELECT new_session_id, visitor_id, id AS cut_edge_id
            FROM idle_cut_edges ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT id AS new_session_id, visitor_id, NULL AS cut_edge_id
            FROM sessions ORDER BY started_at ASC, id ASC LIMIT 1
            """
        ).fetchone()
    if row is None:
        return {
            "session_id": None,
            "visitor_id": None,
            "cut_edge_id": None,
            "from_audit": False,
        }
    return {
        "session_id": row["new_session_id"],
        "visitor_id": row["visitor_id"],
        "cut_edge_id": row["cut_edge_id"],
        "from_audit": from_audit,
    }


def _audit_row_reasons(conn, audit: dict, edge) -> list:
    reasons = []

    if edge is None:
        reasons.append(
            {"kind": "orphan_audit", "detail": "源审计找不到对应切边"}
        )
    else:
        for field, label in (
            ("visitor_id", "访客"),
            ("old_session_id", "旧源会话"),
            ("new_session_id", "新源会话"),
            ("trigger_hit_id", "触发击点"),
        ):
            if audit[field] != edge[field]:
                reasons.append(
                    {
                        "kind": "edge_field_mismatch",
                        "field": field,
                        "audit": audit[field],
                        "edge": edge[field],
                        "detail": f"源审计{label}与切边不一致",
                    }
                )

    if audit["new_session_id"] == audit["old_session_id"]:
        reasons.append(
            {"kind": "source_stale", "detail": "新源仍为切前会话"}
        )

    for field, sid in (
        ("old", audit["old_session_id"]),
        ("new", audit["new_session_id"]),
    ):
        sess = conn.execute(
            "SELECT visitor_id FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        if sess is None:
            reasons.append(
                {
                    "kind": f"{field}_session_missing",
                    "session_id": sid,
                    "detail": f"{field} 源会话不存在",
                }
            )
        elif sess["visitor_id"] != audit["visitor_id"]:
            reasons.append(
                {
                    "kind": f"{field}_session_visitor_mismatch",
                    "session_id": sid,
                    "detail": f"{field} 源会话不属于该访客",
                }
            )

    old_ids = ordered_member_ids(conn, audit["old_session_id"])
    if not old_ids:
        reasons.append(
            {"kind": "old_members_missing", "detail": "切前会话无成员"}
        )
    else:
        if member_digest(old_ids) != audit["old_members_hash"]:
            reasons.append(
                {
                    "kind": "old_members_hash_mismatch",
                    "expected": member_digest(old_ids),
                    "audited": audit["old_members_hash"],
                    "detail": "切前会话成员哈希与源审计不一致",
                }
            )
        if len(old_ids) != audit["old_member_count"]:
            reasons.append(
                {
                    "kind": "old_member_count_mismatch",
                    "expected": len(old_ids),
                    "audited": audit["old_member_count"],
                }
            )
        latest_proof = _latest_proof_ids(conn, audit["old_session_id"])
        if latest_proof is not None and latest_proof != old_ids:
            reasons.append(
                {
                    "kind": "old_proof_mismatch",
                    "detail": "切前会话核证成员与当前成员物化不一致",
                }
            )

    earliest_proof = _earliest_proof_ids(conn, audit["new_session_id"])
    if earliest_proof is None:
        reasons.append(
            {
                "kind": "new_proof_missing",
                "detail": "新源会话缺少切分时刻成员核证",
            }
        )
    else:
        if member_digest(earliest_proof) != audit["new_members_hash"]:
            reasons.append(
                {
                    "kind": "new_members_hash_mismatch",
                    "expected": member_digest(earliest_proof),
                    "audited": audit["new_members_hash"],
                    "detail": "新源切分时刻成员哈希与源审计不一致",
                }
            )
        if len(earliest_proof) != audit["new_member_count"]:
            reasons.append(
                {
                    "kind": "new_member_count_mismatch",
                    "expected": len(earliest_proof),
                    "audited": audit["new_member_count"],
                }
            )
        if audit["trigger_hit_id"] not in earliest_proof:
            reasons.append(
                {
                    "kind": "trigger_not_in_new_source",
                    "detail": "触发击点不在新源切分时刻成员中",
                }
            )

    return reasons


def verify(conn, visitor_id: str | None = None, cut_edge_id: int | None = None) -> dict:
    """织机源审计只读核证：审计行 × 切边 × 会话成员物化三方对撞。"""
    all_edges = {
        r["id"]: dict(r) for r in conn.execute("SELECT * FROM idle_cut_edges")
    }
    edges = [dict(r) for r in conn.execute(
        "SELECT * FROM idle_cut_edges ORDER BY id"
    )]
    audits = [
        dict(r) for r in conn.execute(
            "SELECT * FROM loom_source_audit ORDER BY id"
        )
    ]
    if visitor_id is not None:
        edges = [e for e in edges if e["visitor_id"] == visitor_id]
        audits = [a for a in audits if a["visitor_id"] == visitor_id]
    if cut_edge_id is not None:
        edges = [e for e in edges if e["id"] == cut_edge_id]
        audits = [a for a in audits if a["cut_edge_id"] == cut_edge_id]

    rows = []
    divergences = []
    if cut_edge_id is not None and cut_edge_id not in all_edges:
        divergences.append(
            {
                "kind": "unknown_cut_edge",
                "cut_edge_id": cut_edge_id,
                "detail": "指定切边不存在，无法核证源审计",
            }
        )
    audit_by_edge = {}
    for audit in audits:
        edge = all_edges.get(audit["cut_edge_id"])
        reasons = _audit_row_reasons(conn, audit, edge)
        row = dict(audit)
        row["aligned"] = not reasons
        row["reasons"] = reasons
        rows.append(row)
        audit_by_edge.setdefault(audit["cut_edge_id"], []).append(audit)
        if reasons:
            divergences.append(
                {
                    "kind": "audit_row_misaligned",
                    "audit_id": audit["id"],
                    "cut_edge_id": audit["cut_edge_id"],
                    "visitor_id": audit["visitor_id"],
                    "reasons": reasons,
                }
            )

    for edge in edges:
        if edge["id"] not in audit_by_edge:
            divergences.append(
                {
                    "kind": "missing_audit",
                    "cut_edge_id": edge["id"],
                    "visitor_id": edge["visitor_id"],
                    "detail": "切窗成功但缺少织机源审计行",
                }
            )

    for edge_id, group in audit_by_edge.items():
        if len(group) > 1:
            divergences.append(
                {
                    "kind": "duplicate_audit",
                    "cut_edge_id": edge_id,
                    "audit_ids": [a["id"] for a in group],
                    "detail": "同一在切边存在多行源审计",
                }
            )

    source = resolve_source(conn)
    ok = not divergences
    return {
        "ok": ok,
        "aligned": ok,
        "verdict": "pass" if ok else "fail",
        "record_form": RECORD_FORM,
        "loom_source_session_id": source["session_id"],
        "loom_source": source,
        "rows": rows,
        "audit_rows": rows,
        "cut_edges": edges,
        "divergences": divergences,
    }
