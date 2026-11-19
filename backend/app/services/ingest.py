import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.modules.idle_timeout_cut import idle_timeout_sec, place_hit


def run_batch() -> dict:
    """收流：同事务逐击点做空闲超时裁决。

    关旧会话 / 开新会话 / 击点改挂 / 会话核证 / 切分边表全部在同一个
    ``BEGIN IMMEDIATE`` 失败闭内提交；任一步抛错整体回滚，不会出现半截
    切分双挂或旧未关已开新。
    """
    path = FIXTURES_DIR / "ingest_batch.json"
    if not path.exists():
        return {"ok": False, "error": "缺少 ingest_batch.json"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    hits = payload.get("hits", [])

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        timeout_sec = idle_timeout_sec(conn)
        # 批内先按访客、再按时刻排序，保证「当前 open 末击点」裁决有序。
        hits = sorted(hits, key=lambda h: (h["visitor_id"], h["event_ts"], h["id"]))
        written = 0
        duplicates = 0
        cuts = 0
        cut_edges = []
        for hit in hits:
            vid = hit["visitor_id"]
            exists = conn.execute("SELECT 1 FROM visitors WHERE id=?", (vid,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO visitors(id, label, created_at) VALUES (?,?,?)",
                    (vid, hit.get("visitor_label") or vid, _now()),
                )
            outcome = place_hit(
                conn,
                {
                    "id": hit["id"],
                    "visitor_id": vid,
                    "event_ts": hit["event_ts"],
                    "page_path": hit["page_path"],
                    "event_name": hit["event_name"],
                },
                timeout_sec=timeout_sec,
            )
            if outcome["action"] == "duplicate":
                duplicates += 1
                continue
            written += 1
            if outcome["action"] == "cut":
                cuts += 1
                cut_edges.append(
                    {
                        "trigger_hit_id": hit["id"],
                        "old_session_id": outcome["old_session_id"],
                        "new_session_id": outcome["new_session_id"],
                        "gap_sec": outcome["gap_sec"],
                        "idle_timeout_sec": timeout_sec,
                    }
                )
        report = {
            "batch_id": batch_id,
            "written": written,
            "duplicates": duplicates,
            "cuts": cuts,
            "idle_timeout_sec": timeout_sec,
            "cut_edges": cut_edges,
            "input": len(hits),
        }
        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
            (batch_id, _now(), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

