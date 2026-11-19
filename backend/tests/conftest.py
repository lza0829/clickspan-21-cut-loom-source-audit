import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

# 首次 import 前给一个占位目录；每个用例再 monkeypatch 模块级 DB 路径。
import tempfile

import os

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="clickspan-init-"))
os.environ.setdefault("FIXTURES_DIR", str(BACKEND.parent / "fixtures"))


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """每用例独立 SQLite 库：直接替换 app.db 的模块级路径常量。"""
    from app import db as db_mod

    data_dir = tmp_path / "data"
    monkeypatch.setattr(db_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(db_mod, "DB_PATH", data_dir / "clickspan.db")

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        from app import seed as seed_mod

        yield client, db_mod, seed_mod
