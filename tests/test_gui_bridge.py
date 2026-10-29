from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest
from food_processes import Application
from spike_gui import Bridge


def req(op, **args):
    return {"api_version": "0.1", "request_id": "test", "op": op, "args": args}


def test_bridge_parity_and_diagnostics(tmp_path):
    reference, bridge = Application(tmp_path), Bridge(Application(tmp_path))
    request = req("module.info", module_id="humid-air")
    assert bridge.request(request) == reference.request(request)
    bad = bridge.request(req("session.state", session_id="missing"))
    assert bad["outcome"] == "error" and bad["diagnostics"][0]["code"] == "session.not_found"


def test_bridge_serializes_same_revision(tmp_path):
    bridge = Bridge(Application(tmp_path))
    sid = bridge.request(req("session.create", module_id="humid-air"))["data"]["session_id"]
    gate = threading.Barrier(2)
    def call(point):
        gate.wait(timeout=5)
        return bridge.request(req("session.command", session_id=sid, expected_revision=0, action="select_region", params={"region_id": "schematic-area-a", "point_id": point}))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(call, ["a", "b"]))
    assert sorted(r["outcome"] for r in results) == ["error", "ok"]
    assert next(r for r in results if r["outcome"] == "error")["diagnostics"][0]["code"] == "revision.conflict"


def test_bridge_unexpected_error_does_not_expose_internal_text():
    class Broken:
        def request(self, request):
            raise RuntimeError("secret internal exception")
    result = Bridge(Broken()).request(req("session.create", module_id="humid-air"))
    assert result["diagnostics"][0]["code"] == "bridge.internal_error"
    assert "secret" not in json.dumps(result)


def test_html_is_local_and_no_physical_computation():
    html = Path("spike.html").read_text(encoding="utf-8-sig")
    assert "pywebviewready" in html and "storage.write" in html and "expected_generation" in html
    assert "http://" not in html and "https://" not in html
    assert "pywebview.api.request" in html
    assert "Math.exp" not in html and "Math.log" not in html
    assert "Условный учебный пример. Не является показанием реальной установки." in html


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js отсутствует: DOM-тест прототипа недоступен")
def test_ui_logic_with_real_python_bridge_without_window(tmp_path):
    process = subprocess.Popen(["node", "tests/gui_dom_test.js"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    bridge = Bridge(Application(tmp_path))
    completed = False
    try:
        for line in process.stdout:
            event = json.loads(line)
            if event["type"] == "done":
                completed = True
                break
            if event["type"] == "restart":
                bridge = Bridge(Application(tmp_path))
                result = {"ok": True}
            else:
                result = bridge.request(event["payload"])
            process.stdin.write(json.dumps(result, ensure_ascii=False) + "\n")
            process.stdin.flush()
    finally:
        process.stdin.close()
        process.wait(timeout=30)
        process.stdout.close()
        stderr = process.stderr.read()
        process.stderr.close()
    assert process.returncode == 0 and completed, stderr


def test_preflight_reports_missing_webview2(monkeypatch):
    import types
    import spike_gui
    class FakeKey:
        def __enter__(self): return self
        def __exit__(self, *args): return None
    fake = types.SimpleNamespace(HKEY_LOCAL_MACHINE=1, HKEY_CURRENT_USER=2)
    def open_key(hive, path):
        if "NET Framework" in path:
            return FakeKey()
        raise FileNotFoundError(path)
    fake.OpenKey = open_key
    fake.QueryValueEx = lambda key, name: (533509, 0)
    monkeypatch.setattr(spike_gui.sys, "platform", "win32")
    monkeypatch.setattr(spike_gui.platform, "machine", lambda: "AMD64")
    monkeypatch.setitem(sys.modules, "winreg", fake)
    with pytest.raises(RuntimeError, match="WebView2 Runtime"):
        spike_gui.require_webview2()


def test_default_storage_outside_repository():
    from food_processes.core import default_storage
    root = Path.cwd().resolve()
    saved = default_storage().resolve()
    assert saved != root and root not in saved.parents
