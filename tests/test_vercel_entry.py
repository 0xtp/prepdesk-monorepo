"""The Vercel shim restores the original path whatever path the function receives."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("vercel_index", Path(__file__).resolve().parents[1] / "api" / "index.py")


def _restore():
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._restore


def test_restore_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/v.db")
    r = _restore()
    s = r({"type": "http", "path": "/api/index", "query_string": b"__p=/asset/3&tf=1h"})
    assert s["path"] == "/asset/3" and s["query_string"] == b"tf=1h"
    assert r({"type": "http", "path": "/api/index", "query_string": b"__p=/"})["path"] == "/"
    assert r({"type": "http", "path": "/api/index/healthz", "query_string": b""})["path"] == "/healthz"
    assert r({"type": "http", "path": "/track", "query_string": b""})["path"] == "/track"
    assert r({"type": "http", "path": "/x", "query_string": b"__p=static%2Fapp.css"})["path"] == "/static/app.css"
