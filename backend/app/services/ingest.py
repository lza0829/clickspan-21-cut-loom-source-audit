import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.modules.idle_timeout_cut import idle_timeout_sec, place_hit
from app.modules.loom_source_audit import resolve_source, verify


def run_batch() -> dict:
    """收流：同事务逐击点做空闲超时裁决。

    关旧会话 / 开新会话 / 击点改挂 / 会话核证 / 切分边表 / 织机源审计全部在
    同一个 ``BEGIN IMMEDIATE`` 失败闭内提交；任一步抛错整体回滚，不会出现
    半截切分双挂、旧未关已开新或切窗成功而源审计缺行。提交前再对撞一遍
    本批切边的源审计与成员物化，缺一即回滚。
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
                        "cut_edge_id": outcome["cut_edge_id"],
                        "gap_sec": outcome["gap_sec"],
                        "idle_timeout_sec": timeout_sec,
                    }
                )
        # 提交前对撞：本批每个切边都必须有对齐的源审计行。
        for edge in cut_edges:
            check = verify(
                conn, visitor_id=None, cut_edge_id=edge["cut_edge_id"]
            )
            if not check["ok"]:
                raise RuntimeError(
                    "切窗源审计核证失败："
                    + json.dumps(check["divergences"], ensure_ascii=False)
                )
            if check["loom_source_session_id"] != edge["new_session_id"]:
                raise RuntimeError("切窗后织机顶栏源未切到新承接会话")
        source = resolve_source(conn)
        report = {
            "batch_id": batch_id,
            "written": written,
            "duplicates": duplicates,
            "cuts": cuts,
            "idle_timeout_sec": timeout_sec,
            "cut_edges": cut_edges,
            "loom_source_session_id": source["session_id"],
            "loom_source_cut_edge_id": source["cut_edge_id"],
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
