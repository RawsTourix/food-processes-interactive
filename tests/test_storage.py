import copy
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from food_processes import Application
from food_processes import storage
from tests.test_headless import call, ok, error, new, command, save, conditions
from tests.test_answers import setup, put, check


def test_independent_process_ids_unique(tmp_path):
    request = json.dumps({"api_version": "0.1", "request_id": "x", "op": "session.create", "args": {"module_id": "humid-air"}}) + "\n"
    def run(_):
        result = subprocess.run([sys.executable, "-m", "food_processes", "--storage-dir", str(tmp_path)], input=request, encoding="utf-8", capture_output=True, check=True)
        return json.loads(result.stdout)["data"]["session_id"]
    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(run, range(8)))
    assert len(set(ids)) == 8
    assert all(s.startswith("s-") and len(s) == 34 for s in ids)


def test_two_real_processes_conflict_and_preserve_winner(tmp_path):
    app = Application(tmp_path)
    sid = new(app, "humid-air")
    command(app, sid, 0, "select_region", region_id="schematic-area-a", point_id="original")
    initial = save(app, sid, 1)
    first = ok(app, "storage.write", document=initial, target="shared.json", expected_generation=None)
    assert first["generation"].startswith("g-")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    procs = [subprocess.Popen([sys.executable, "-m", "tests.storage_worker", str(tmp_path), "write", point], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env) for point in ("A", "B")]
    try:
        ready = [json.loads(proc.stdout.readline()) for proc in procs]
        assert all(item["ready"] for item in ready)
        assert ready[0]["generation"] == ready[1]["generation"] == first["generation"]
        for proc in procs:
            proc.stdin.write("go\n")
            proc.stdin.flush()
        results = [(point, json.loads(proc.stdout.readline())) for point, proc in zip(("A", "B"), procs)]
        winners = [(p, r) for p, r in results if r["outcome"] == "ok"]
        losers = [(p, r) for p, r in results if r["outcome"] == "error"]
        assert len(winners) == len(losers) == 1
        assert losers[0][1]["diagnostics"][0]["code"] == "storage.conflict"
        reread = ok(app, "storage.read", target="shared.json")
        assert reread["document"]["revision"] == 2
        assert reread["document"]["module_data"]["selection"]["point_id"] == winners[0][0]
        assert reread["generation"] == winners[0][1]["data"]["generation"]
        after = Application(tmp_path)
        assert ok(after, "session.restore", document=reread["document"])["state"]["selection"]["point_id"] == winners[0][0]
    finally:
        for proc in procs:
            proc.stdin.close()
            proc.wait(timeout=20)
            proc.stdout.close()
            stderr = proc.stderr.read()
            proc.stderr.close()
            assert proc.returncode == 0, stderr



def test_generation_changes_on_identical_writes_and_requires_token(tmp_path):
    app = Application(tmp_path)
    sid = new(app, "humid-air")
    doc = save(app, sid, 0)
    error(app, "storage.generation_required", "storage.write", document=doc, target="x.json")
    a = ok(app, "storage.write", document=doc, target="x.json", expected_generation=None)
    error(app, "storage.conflict", "storage.write", document=doc, target="x.json", expected_generation=None)
    b = ok(app, "storage.write", document=doc, target="x.json", expected_generation=a["generation"])
    assert b["generation"] != a["generation"]
    error(app, "storage.conflict", "storage.write", document=doc, target="x.json", expected_generation=a["generation"])
    assert ok(app, "storage.read", target="x.json")["generation"] == b["generation"]


def test_session_ids_and_legacy_documents(tmp_path):
    app = Application(tmp_path)
    sid = new(app, "heat-exchanger")
    command(app, sid, 0, "set_conditions", **conditions())
    command(app, sid, 1, "capture_snapshot")
    v2 = save(app, sid, 2)
    v1 = copy.deepcopy(v2)
    v1["session_id"] = "s1"
    v1["module_data_version"] = 1
    del v1["module_data"]["conditions_version"], v1["module_data"]["answers"]
    for snapshot in v1["module_data"]["snapshots"]:
        del snapshot["conditions_version"]
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(v1), encoding="utf-8")
    original = path.read_bytes()
    legacy = ok(app, "storage.read", target="legacy.json")
    assert path.read_bytes() == original
    assert legacy["generation"].startswith("legacy-")
    assert legacy["document"]["module_data_version"] == 2
    other = Application(tmp_path)
    assert ok(other, "session.restore", document=legacy["document"])["session_id"] == "s1"
    ok(app, "storage.write", target="legacy.json", document=legacy["document"], expected_generation=legacy["generation"])
    assert ok(app, "storage.read", target="legacy.json")["generation"].startswith("g-")
    assert ok(Application(tmp_path), "session.restore", document=v2)["state"] == v2["module_data"]


def test_answers_and_checks_survive_generation_roundtrip(tmp_path):
    app = Application(tmp_path)
    setup(app)
    put(app, 2, n=10)
    check(app, 3)
    sid = next(iter(app.sessions))
    before = save(app, sid, 4)
    token = ok(app, "storage.write", target="answers.json", document=before, expected_generation=None)["generation"]
    opened = ok(Application(tmp_path), "storage.read", target="answers.json")
    assert opened["generation"] == token and opened["document"] == before
    answer = opened["document"]["module_data"]["answers"][0]
    assert answer["checks"][0]["status"] == "does_not_match"
    assert answer["context_ref"]["snapshot_id"] == "hx-s1"


@pytest.mark.parametrize("where", ["before_temp", "during_json", "fsync", "before_replace", "replace"])
def test_faults_keep_original_and_cleanup(tmp_path, monkeypatch, where):
    app = Application(tmp_path)
    sid = new(app, "humid-air")
    initial = save(app, sid, 0)
    token = ok(app, "storage.write", target="old.json", document=initial, expected_generation=None)["generation"]
    original = (tmp_path / "old.json").read_bytes()
    command(app, sid, 0, "select_region", region_id="schematic-area-a", point_id="new")
    changed = save(app, sid, 1)
    def broken(*args, **kwargs):
        raise OSError("simulated failure")
    if where == "before_temp":
        monkeypatch.setattr(storage.tempfile, "NamedTemporaryFile", broken)
    elif where == "during_json":
        def partial(payload, f, **kwargs):
            f.write('{"partial":')
            raise OSError("simulated partial write")
        monkeypatch.setattr(storage.json, "dump", partial)
    elif where == "fsync":
        monkeypatch.setattr(storage.os, "fsync", broken)
    elif where == "before_replace":
        def complete_then_fail(src, dst):
            assert json.loads(open(src, encoding="utf-8").read())["storage_format"] == "food-processes-file"
            raise OSError("simulated stop after completing temporary file")
        monkeypatch.setattr(storage.os, "replace", complete_then_fail)
    else:
        monkeypatch.setattr(storage.os, "replace", broken)
    error(app, "storage.write_failed", "storage.write", document=changed, target="old.json", expected_generation=token)
    assert (tmp_path / "old.json").read_bytes() == original
    assert not list(tmp_path.glob(".session-*.tmp"))


def test_uncertain_failure_after_replace_requires_reread(tmp_path, monkeypatch):
    app = Application(tmp_path)
    sid = new(app, "humid-air")
    doc = save(app, sid, 0)
    token = ok(app, "storage.write", target="x.json", document=doc, expected_generation=None)["generation"]
    real_replace = os.replace
    def replaced_then_error(src, dst):
        real_replace(src, dst)
        raise OSError("simulated loss of acknowledgement")
    monkeypatch.setattr(storage.os, "replace", replaced_then_error)
    error(app, "storage.write_failed", "storage.write", document=doc, target="x.json", expected_generation=token)
    current = ok(app, "storage.read", target="x.json")
    assert current["generation"] != token
    error(app, "storage.conflict", "storage.write", document=doc, target="x.json", expected_generation=token)


def test_lock_released_after_process_kill(tmp_path):
    proc = subprocess.Popen([sys.executable, "-m", "tests.storage_worker", str(tmp_path), "hold"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    try:
        assert json.loads(proc.stdout.readline())["ready"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()
    app = Application(tmp_path)
    sid = new(app, "humid-air")
    result = ok(app, "storage.write", target="shared.json", document=save(app, sid, 0), expected_generation=None)
    assert result["generation"].startswith("g-")
    assert (tmp_path / ".shared.json.lock").is_file()


def test_simultaneous_read_write_returns_complete_documents(tmp_path):
    app = Application(tmp_path)
    sid = new(app, "humid-air")
    command(app, sid, 0, "select_region", region_id="schematic-area-a", point_id="v0")
    ok(app, "storage.write", target="shared.json", document=save(app, sid, 1), expected_generation=None)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    processes = [subprocess.Popen([sys.executable, "-m", "tests.storage_worker", str(tmp_path), mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env) for mode in ("writer-loop", "reader-loop")]
    try:
        assert all(json.loads(p.stdout.readline())["ready"] for p in processes)
        for i in range(1, 11):
            for proc in processes:
                proc.stdin.write("step\n")
                proc.stdin.flush()
            writer, reader = [json.loads(p.stdout.readline()) for p in processes]
            assert writer["revision"] == i + 1
            assert reader["point"].startswith("v")
            assert int(reader["point"][1:]) + 1 == reader["revision"]
    finally:
        for proc in processes:
            proc.stdin.close()
            proc.wait(timeout=20)
            proc.stdout.close()
            stderr = proc.stderr.read()
            proc.stderr.close()
            assert proc.returncode == 0, stderr


def test_corrupt_or_foreign_file_is_not_overwritten(tmp_path):
    app = Application(tmp_path)
    a = new(app, "humid-air")
    b = new(app, "humid-air")
    original = save(app, a, 0)
    token = ok(app, "storage.write", target="shared.json", document=original, expected_generation=None)["generation"]
    error(app, "storage.session_mismatch", "storage.write", target="shared.json", document=save(app, b, 0), expected_generation=token)
    assert ok(app, "storage.read", target="shared.json")["document"] == original
    bad = tmp_path / "broken.json"
    bad.write_text('{"storage_format":"food-processes-file","storage_version":2}', encoding="utf-8")
    before = bad.read_bytes()
    error(app, "storage.unsupported", "storage.read", target="broken.json")
    error(app, "storage.unsupported", "storage.write", target="broken.json", document=original, expected_generation=None)
    assert bad.read_bytes() == before
    assert ok(app, "session.state", session_id=a)["revision"] == 0


@pytest.mark.parametrize("module", ["heat-exchanger", "humid-air"])
def test_legacy_plain_document_is_not_changed_until_explicit_write(tmp_path, module):
    app = Application(tmp_path)
    sid = new(app, module)
    doc = save(app, sid, 0)
    path = tmp_path / "old.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    old = path.read_bytes()
    opened = ok(app, "storage.read", target="old.json")
    assert path.read_bytes() == old
    assert opened["generation"].startswith("legacy-")
    assert opened["document"] == doc
    failure = call(app, "session.restore", document=opened["document"])
    assert failure["diagnostics"][0]["code"] == "session.already_exists"
    assert ok(app, "session.state", session_id=sid)["revision"] == 0


def test_cli_and_python_interoperate_with_generation_tokens(tmp_path):
    app = Application(tmp_path)
    sid = new(app, "humid-air")
    doc = save(app, sid, 0)
    req = {"api_version": "0.1", "request_id": "1", "op": "storage.write", "args": {"target": "cli.json", "document": doc, "expected_generation": None}}
    proc = subprocess.run([sys.executable, "-m", "food_processes", "--storage-dir", str(tmp_path)], input=json.dumps(req) + "\n", encoding="utf-8", capture_output=True, check=True)
    written = json.loads(proc.stdout)
    assert written["outcome"] == "ok"
    opened = ok(app, "storage.read", target="cli.json")
    assert opened["generation"] == written["data"]["generation"] and opened["document"] == doc
    changed = ok(app, "storage.write", target="cli.json", document=doc, expected_generation=opened["generation"])
    stale = subprocess.run([sys.executable, "-m", "food_processes", "--storage-dir", str(tmp_path)], input=json.dumps({**req, "args": {**req["args"], "expected_generation": opened["generation"]}}) + "\n", encoding="utf-8", capture_output=True, check=True)
    assert json.loads(stale.stdout)["diagnostics"][0]["code"] == "storage.conflict"
    assert ok(app, "storage.read", target="cli.json")["generation"] == changed["generation"]


def test_busy_lock_times_out_and_does_not_write(tmp_path):
    proc = subprocess.Popen([sys.executable, "-m", "tests.storage_worker", str(tmp_path), "hold"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    try:
        assert json.loads(proc.stdout.readline())["ready"]
        with pytest.raises(Exception) as caught:
            with storage.file_lock(tmp_path, "shared.json", timeout=0.05):
                raise AssertionError("lock must not be acquired")
        assert caught.value.diagnostic["code"] == "storage.lock_timeout"
        assert not (tmp_path / "shared.json").exists()
    finally:
        proc.stdin.write("release\n")
        proc.stdin.flush()
        proc.stdin.close()
        proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()
    assert proc.returncode == 0
