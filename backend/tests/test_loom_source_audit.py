"""织机源审计 / 珠串分段投影 / 顶栏源互证（功能题 21）。

覆盖两条硬约束路径：
* 「（超）超时切开后源 = 新会话且珠串两段」——切窗标成功时同事务写源审计，
  顶栏源取切后承接会话，珠串按会话分段不跨切边。
* 「人为源审计与切边分叉须失败」——篡改审计行 / 删审计行后核证接口与
  收流提交前对撞都必须报失败，绝不假成功。
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import connect
from app.modules.loom_source_audit import member_digest, verify


def _source_audit_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM loom_source_audit ORDER BY id")]


def test_cut_writes_source_audit_and_topbar_points_to_new_session(temp_db):
    client, _, _ = temp_db
    conn = connect()
    try:
        sessions = conn.execute(
            "SELECT id, status, started_at FROM sessions WHERE visitor_id='V-1001' ORDER BY started_at"
        ).fetchall()
        edge = conn.execute("SELECT * FROM idle_cut_edges").fetchone()
        audit = conn.execute("SELECT * FROM loom_source_audit").fetchone()
    finally:
        conn.close()

    assert len(sessions) == 2
    assert edge is not None and audit is not None

    # 源审计与切边逐键同源，新源 = 切后承接会话，不再停在切前 id。
    assert audit["cut_edge_id"] == edge["id"]
    assert audit["visitor_id"] == "V-1001"
    assert audit["old_session_id"] == sessions[0]["id"]
    assert audit["new_session_id"] == sessions[1]["id"]
    assert audit["trigger_hit_id"] == "H4"
    assert audit["new_session_id"] != sessions[0]["id"]

    # 两侧成员哈希对齐成员物化：旧段 H1/H2/H3，新段自 H4 起。
    assert audit["old_members_hash"] == member_digest(["H1", "H2", "H3"])
    assert audit["new_members_hash"] == member_digest(["H4"])
    assert audit["old_member_count"] == 3 and audit["new_member_count"] == 1

    overview = client.get("/api/overview").json()
    assert overview["loom_source_session_id"] == sessions[1]["id"]
    assert overview["loom_source_session_id"] != sessions[0]["id"]
    assert overview["loom_source_audit_rows"] == 1
    assert overview["loom_source_from_audit"] is True

    audit_api = client.get("/api/loom-source-audit").json()
    assert audit_api["ok"] is True and audit_api["aligned"] is True
    assert audit_api["loom_source_session_id"] == sessions[1]["id"]
    assert len(audit_api["rows"]) == 1
    row = audit_api["rows"][0]
    assert row["aligned"] is True and row["reasons"] == []
    assert row["new_session_id"] == sessions[1]["id"]


def test_beads_segment_per_session_no_cross_cut(temp_db):
    client, _, _ = temp_db
    conn = connect()
    try:
        sessions = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-1001' ORDER BY started_at"
        ).fetchall()
    finally:
        conn.close()
    old_sid, new_sid = sessions[0]["id"], sessions[1]["id"]

    projection = client.get("/api/strands", params={"visitor_id": "V-1001"}).json()
    assert projection["aligned"] is True
    assert projection["strand_order"] == "session_scoped"
    assert [s["session_id"] for s in projection["segments"]] == [old_sid, new_sid]
    assert projection["segments"][0]["member_ids"] == ["H1", "H2", "H3"]
    assert projection["segments"][1]["member_ids"] == ["H4"]
    assert projection["crossing_member_ids"] == []
    assert projection["missing_member_ids"] == []

    # 会话详情只回本段击点：旧段不含 H4，新段不含 H1/H2/H3。
    old_detail = client.get(f"/api/sessions/{old_sid}").json()
    new_detail = client.get(f"/api/sessions/{new_sid}").json()
    assert [h["id"] for h in old_detail["hits"]] == ["H1", "H2", "H3"]
    assert [h["id"] for h in new_detail["hits"]] == ["H4"]
    assert "H4" not in [h["id"] for h in old_detail["hits"]]
    assert new_detail["loom_source_session_id"] == new_sid
    assert new_detail["segment_count"] == 2
    assert new_detail["segments_aligned"] is True


def test_ingest_cut_keeps_source_audit_and_new_source(temp_db, monkeypatch, tmp_path):
    """超超时切开后：切窗成功、源审计跟切、顶栏源 = 新承接会话、珠串两段。"""
    import app.services.ingest as ingest_mod

    client, _, _ = temp_db
    # 夹具：V-1004 首点在 12:00，次点在 16:00，间隙 4h ≫ 超时 1800s。
    fixture_path = Path(__file__).resolve().parent.parent.parent / "fixtures" / "ingest_cut_source.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert payload["hits"][-1]["visitor_id"] == "V-1004"
    (tmp_path / "ingest_batch.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(ingest_mod, "FIXTURES_DIR", tmp_path)

    res = client.post("/api/ingest/run").json()

    assert res["ok"] is True, res
    report = res["report"]
    assert report["cuts"] == 1
    edge_out = report["cut_edges"][0]
    assert report["loom_source_session_id"] == edge_out["new_session_id"]

    conn = connect()
    try:
        v4 = conn.execute(
            "SELECT id, status FROM sessions WHERE visitor_id='V-1004' ORDER BY started_at"
        ).fetchall()
        audit = conn.execute(
            "SELECT * FROM loom_source_audit WHERE visitor_id='V-1004'"
        ).fetchall()
    finally:
        conn.close()
    assert [r["status"] for r in v4] == ["closed", "open"]
    assert len(audit) == 1
    assert audit[0]["new_session_id"] == v4[1]["id"]
    assert audit[0]["old_session_id"] == v4[0]["id"]

    overview = client.get("/api/overview").json()
    assert overview["loom_source_session_id"] == v4[1]["id"]

    projection = client.get("/api/strands", params={"visitor_id": "V-1004"}).json()
    assert projection["aligned"] is True
    assert [s["session_id"] for s in projection["segments"]] == [v4[0]["id"], v4[1]["id"]]
    assert projection["segments"][0]["member_ids"] == ["H30"]
    assert projection["segments"][1]["member_ids"] == ["H31"]

    audit_api = client.get("/api/loom-source-audit", params={"visitor_id": "V-1004"}).json()
    assert audit_api["ok"] is True
    assert audit_api["loom_source_session_id"] == v4[1]["id"]


def test_tampered_source_audit_diverges_and_fails(temp_db):
    client, _, _ = temp_db
    conn = connect()
    try:
        edge = conn.execute("SELECT * FROM idle_cut_edges").fetchone()
        other = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-1002' LIMIT 1"
        ).fetchone()["id"]
        # 库层 CHECK 先挡「新源 = 切前会话」这种最直接的篡改。
        try:
            conn.execute(
                "UPDATE loom_source_audit SET new_session_id=? WHERE cut_edge_id=?",
                (edge["old_session_id"], edge["id"]),
            )
            raise AssertionError("库层应拒绝把源审计新源改成切前会话")
        except Exception as exc:  # sqlite3.IntegrityError
            assert "CHECK" in str(exc) or "Integrity" in type(exc).__name__
        conn.rollback()

        # 人为把源审计新源指向别的会话：模拟审计与切边分叉。
        conn.execute(
            "UPDATE loom_source_audit SET new_session_id=? WHERE cut_edge_id=?",
            (other, edge["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    audit_api = client.get("/api/loom-source-audit").json()
    assert audit_api["ok"] is False
    assert audit_api["verdict"] == "fail"
    kinds = {d["kind"] for d in audit_api["divergences"]}
    assert "audit_row_misaligned" in kinds
    row = audit_api["rows"][0]
    assert row["aligned"] is False
    reason_kinds = {r["kind"] for r in row["reasons"]}
    assert "edge_field_mismatch" in reason_kinds

    # 顶栏源绝不跟随被篡改的审计行：退回切边的新承接会话。
    overview = client.get("/api/overview").json()
    assert overview["loom_source_session_id"] == edge["new_session_id"]
    assert overview["loom_source_session_id"] != edge["old_session_id"]
    assert overview["loom_source_session_id"] != other


def test_missing_source_audit_row_fails(temp_db):
    client, _, _ = temp_db
    conn = connect()
    try:
        conn.execute("DELETE FROM loom_source_audit")
        conn.commit()
        edge_id = conn.execute("SELECT id FROM idle_cut_edges").fetchone()["id"]
    finally:
        conn.close()

    audit_api = client.get("/api/loom-source-audit").json()
    assert audit_api["ok"] is False
    assert any(
        d["kind"] == "missing_audit" and d["cut_edge_id"] == edge_id
        for d in audit_api["divergences"]
    )
    assert client.get("/api/loom-source-audit", params={"cut_edge_id": edge_id}).json()[
        "ok"
    ] is False


def test_member_hash_divergence_fails_temp_db(temp_db):
    client, _, _ = temp_db
    conn = connect()
    try:
        edge = conn.execute("SELECT * FROM idle_cut_edges").fetchone()
        conn.execute(
            "UPDATE loom_source_audit SET old_members_hash=? WHERE cut_edge_id=?",
            (member_digest(["H1"]), edge["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    audit_api = client.get("/api/loom-source-audit").json()
    assert audit_api["ok"] is False
    row = audit_api["rows"][0]
    assert any(r["kind"] == "old_members_hash_mismatch" for r in row["reasons"])


def test_ingest_rolls_back_when_source_audit_cannot_be_written(temp_db, monkeypatch):
    """切窗写不出源审计即整批回滚：不留「切边已落、审计缺行」。"""
    client, _, _ = temp_db
    conn = connect()
    try:
        before = {
            "sessions": conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"],
            "edges": conn.execute("SELECT COUNT(*) AS c FROM idle_cut_edges").fetchone()["c"],
            "audits": conn.execute("SELECT COUNT(*) AS c FROM loom_source_audit").fetchone()["c"],
            "hits": conn.execute("SELECT COUNT(*) AS c FROM hits").fetchone()["c"],
        }
    finally:
        conn.close()

    import app.modules.idle_timeout_cut as cut_mod

    def boom(*_a, **_kw):
        raise RuntimeError("source audit exploded")

    monkeypatch.setattr(cut_mod, "record_cut_source", boom)
    res = client.post("/api/ingest/run").json()
    assert res["ok"] is False
    assert "source audit exploded" in res["error"]

    conn = connect()
    try:
        after = {
            "sessions": conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"],
            "edges": conn.execute("SELECT COUNT(*) AS c FROM idle_cut_edges").fetchone()["c"],
            "audits": conn.execute("SELECT COUNT(*) AS c FROM loom_source_audit").fetchone()["c"],
            "hits": conn.execute("SELECT COUNT(*) AS c FROM hits").fetchone()["c"],
        }
        # 旧 open 仍是 open，未被关掉
        last = conn.execute(
            "SELECT status FROM sessions WHERE visitor_id='V-1001' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert after == before
    assert last["status"] == "open"


def test_verify_reports_cross_cut_mixing(temp_db):
    """人为把新段击点改挂到旧段：分段投影与源审计都必须报失败。"""
    client, _, _ = temp_db
    conn = connect()
    try:
        sessions = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-1001' ORDER BY started_at"
        ).fetchall()
        conn.execute("UPDATE hits SET session_id=? WHERE id='H4'", (sessions[0]["id"],))
        conn.commit()
    finally:
        conn.close()

    projection = client.get("/api/strands", params={"visitor_id": "V-1001"})
    assert projection.status_code == 409
    body = projection.json()["detail"]
    assert body["error"].startswith("珠串分段跨切边混段")
    assert body["projection"]["aligned"] is False

    audit_api = client.get("/api/loom-source-audit").json()
    assert audit_api["ok"] is False
    kinds = {d["kind"] for d in audit_api["divergences"]}
    assert "audit_row_misaligned" in kinds


def test_readme_claim_consistency_check(temp_db):
    """会话详情投影与珠串投影互证：成员并集 = 访客全量、相邻段由切边相连。"""
    client, _, _ = temp_db
    conn = connect()
    try:
        sessions = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-1001' ORDER BY started_at"
        ).fetchall()
    finally:
        conn.close()

    union = []
    for s in sessions:
        detail = client.get(f"/api/sessions/{s['id']}").json()
        union += detail["member_ids"]
        assert detail["segments_aligned"] is True
    assert sorted(union) == ["H1", "H2", "H3", "H4"]

    check = client.get("/api/loom-source-audit").json()
    assert check["ok"] is True
    assert check["loom_source_session_id"] == sessions[1]["id"]
