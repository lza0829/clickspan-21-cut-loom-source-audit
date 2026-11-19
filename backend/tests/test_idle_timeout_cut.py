import json

from app.db import connect
from app.modules.idle_timeout_cut import gap_seconds, idle_timeout_sec, place_hit


def test_gap_seconds_seconds_precision():
    assert gap_seconds("2026-11-17T08:13:00Z", "2026-11-17T09:05:00Z") == 3120
    assert gap_seconds("2026-11-17T08:21:30Z", "2026-11-17T08:24:00Z") == 150
    # 迟到击点：非正间隙，永远不会触发切分
    assert gap_seconds("2026-11-17T10:00:00Z", "2026-11-17T09:59:00Z") == -60


def test_seed_materializes_over_and_under_timeout_segments(temp_db):
    """夹具两串对照：V-1001 超间隙切两段，V-1002 全在窗内保一段。"""
    client, db_mod, _ = temp_db
    conn = connect()
    try:
        v1 = conn.execute(
            "SELECT id, status FROM sessions WHERE visitor_id='V-1001' ORDER BY started_at"
        ).fetchall()
        v2 = conn.execute(
            "SELECT id, status FROM sessions WHERE visitor_id='V-1002' ORDER BY started_at"
        ).fetchall()
        assert len(v1) == 2
        assert [r["status"] for r in v1] == ["closed", "open"]
        assert len(v2) == 1 and v2[0]["status"] == "open"

        edge = conn.execute("SELECT * FROM idle_cut_edges").fetchall()
        assert len(edge) == 1
        e = edge[0]
        assert e["visitor_id"] == "V-1001"
        assert e["old_session_id"] == v1[0]["id"]
        assert e["new_session_id"] == v1[1]["id"]
        assert e["gap_sec"] == 3120
        assert e["idle_timeout_sec"] == 1800
        assert e["trigger_hit_id"] == "H4"

        # 成员不可变快照：两段击点集互斥且覆盖全量
        h1 = conn.execute("SELECT id FROM hits WHERE session_id=? ORDER BY id", (v1[0]["id"],)).fetchall()
        h2 = conn.execute("SELECT id FROM hits WHERE session_id=? ORDER BY id", (v1[1]["id"],)).fetchall()
        assert [r["id"] for r in h1] == ["H1", "H2", "H3"]
        assert [r["id"] for r in h2] == ["H4"]
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM hits WHERE visitor_id='V-1002'"
        ).fetchone()["c"] == 4
    finally:
        conn.close()

    # 只读核证接口字段齐全
    data = client.get("/api/idle-cut-edges").json()
    assert len(data) == 1
    assert set(data[0]) >= {
        "visitor_id",
        "old_session_id",
        "new_session_id",
        "gap_sec",
        "idle_timeout_sec",
        "trigger_hit_id",
    }
    assert client.get("/api/idle-cut-edges", params={"visitor_id": "V-1002"}).json() == []


def test_ingest_cuts_and_attaches_in_one_transaction(temp_db):
    client, _, _ = temp_db
    res = client.post("/api/ingest/run").json()
    assert res["ok"] is True
    report = res["report"]
    assert report["written"] == 3
    assert report["cuts"] == 1
    edge = report["cut_edges"][0]
    assert edge["trigger_hit_id"] == "H9"
    assert edge["gap_sec"] == 3300  # 09:05 → 10:00

    conn = connect()
    try:
        # H9 触发切分：旧 open（H4 段）关闭，H9/H10 并入新段
        v1 = conn.execute(
            "SELECT id, status, hit_count FROM sessions WHERE visitor_id='V-1001' ORDER BY started_at"
        ).fetchall()
        assert len(v1) == 3
        assert [r["status"] for r in v1] == ["closed", "closed", "open"]
        assert [r["hit_count"] for r in v1] == [3, 1, 2]
        attached = conn.execute(
            "SELECT id FROM hits WHERE session_id=? ORDER BY id", (v1[2]["id"],)
        ).fetchall()
        assert [r["id"] for r in attached] == ["H10", "H9"]

        # 新访客首点开新会话，不产生切边
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE visitor_id='V-1003'"
        ).fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM idle_cut_edges").fetchone()["c"] == 2
    finally:
        conn.close()


def test_ingest_is_idempotent_and_no_duplicate_cuts(temp_db):
    client, _, _ = temp_db
    first = client.post("/api/ingest/run").json()["report"]
    second = client.post("/api/ingest/run").json()["report"]
    assert first["written"] == 3 and first["cuts"] == 1
    assert second["written"] == 0 and second["duplicates"] == 3 and second["cuts"] == 0
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM hits").fetchone()["c"] == 11
        assert conn.execute("SELECT COUNT(*) AS c FROM idle_cut_edges").fetchone()["c"] == 2
        # 同访客始终至多一个 open（无双挂）
        rows = conn.execute(
            "SELECT visitor_id, COUNT(*) AS c FROM sessions WHERE status='open' GROUP BY visitor_id"
        ).fetchall()
        assert all(r["c"] == 1 for r in rows)
    finally:
        conn.close()


def test_natural_key_duplicate_does_not_cut(temp_db):
    """同访客同时刻同路径同事件但不同 id：判重前置，不产生空切分。"""
    client, _, _ = temp_db
    conn = connect()
    try:
        before_sessions = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
        before_edges = conn.execute("SELECT COUNT(*) AS c FROM idle_cut_edges").fetchone()["c"]
        # 即便时刻离末击点极远（本应触发切分），查重也先生效
        outcome = place_hit(
            conn,
            {
                "id": "H4-DUP",
                "visitor_id": "V-1001",
                "event_ts": "2026-11-17T09:05:00Z",
                "page_path": "/home",
                "event_name": "view",
            },
        )
        assert outcome["action"] == "duplicate"
        assert conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"] == before_sessions
        assert conn.execute("SELECT COUNT(*) AS c FROM idle_cut_edges").fetchone()["c"] == before_edges
        conn.rollback()
    finally:
        conn.close()


def test_timeout_change_only_affects_future_ingest(temp_db):
    client, _, _ = temp_db
    # 调大到覆盖全部历史间隙：既有 closed 不回溯
    client.put("/api/settings/idle-timeout", json={"idle_timeout_sec": 7200})
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE status='closed'"
        ).fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM idle_cut_edges").fetchone()["c"] == 1

        # 同窗内新击点并入当前 open，不再切
        outcome = place_hit(
            conn,
            {
                "id": "HX",
                "visitor_id": "V-1001",
                "event_ts": "2026-11-17T09:06:00Z",
                "page_path": "/x",
                "event_name": "view",
            },
        )
        assert outcome["action"] == "attach"
        assert idle_timeout_sec(conn) == 7200
    finally:
        conn.rollback()
        conn.close()


def test_failed_batch_rolls_back_no_half_split(temp_db, monkeypatch, tmp_path):
    """注入失败：关旧/开新/边表/核证同失败闭回滚，不留半截切分。"""
    client, db_mod, _ = temp_db
    conn = connect()
    try:
        before_sessions = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
        before_edges = conn.execute("SELECT COUNT(*) AS c FROM idle_cut_edges").fetchone()["c"]
        before_hits = conn.execute("SELECT COUNT(*) AS c FROM hits").fetchone()["c"]
    finally:
        conn.close()

    # 构造一个会触发切分、但在写 session_proofs 时失败的批次夹具
    broken_fixture = tmp_path / "ingest_batch.json"
    broken_fixture.write_text(
        json.dumps(
            {
                "batch_id": "B-broken",
                "hits": [
                    {
                        "id": "HBAD",
                        "visitor_id": "V-1001",
                        "event_ts": "2026-11-17T11:00:00Z",
                        "page_path": "/boom",
                        "event_name": "view",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.services.ingest.FIXTURES_DIR", tmp_path)

    import app.modules.idle_timeout_cut as cut_mod

    def boom(*_a, **_kw):
        raise RuntimeError("proof write exploded")

    monkeypatch.setattr(cut_mod, "_write_proof", boom)

    res = client.post("/api/ingest/run").json()
    assert res["ok"] is False
    assert "exploded" in res["error"]

    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"] == before_sessions
        assert conn.execute("SELECT COUNT(*) AS c FROM idle_cut_edges").fetchone()["c"] == before_edges
        assert conn.execute("SELECT COUNT(*) AS c FROM hits").fetchone()["c"] == before_hits
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM ingest_batches WHERE id='B-broken'"
        ).fetchone()["c"] == 0
        # 旧 open 仍是 open，未被关掉
        row = conn.execute(
            "SELECT status FROM sessions WHERE visitor_id='V-1001' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        assert row["status"] == "open"
    finally:
        conn.close()
