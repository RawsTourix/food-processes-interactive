import copy
import json
import os
import subprocess
import sys

import pytest
from food_processes import Application


def request(op, **args):
    return {"api_version": "0.1", "request_id": "test", "op": op, "args": args}


def send(app, op, **args):
    return app.request(request(op, **args))


def ok(app, op, **args):
    result = send(app, op, **args)
    assert result["outcome"] == "ok", result
    return result["data"]


def failure(app, code, op, **args):
    result = send(app, op, **args)
    assert result["outcome"] == "error", result
    assert result["diagnostics"][0]["code"] == code
    return result


def conditions(temp=80):
    return {"flow": "counter", "hot_in": {"value": temp, "unit": "degC"}, "cold_in": {"value": 20, "unit": "degC"}, "hot_capacity": {"value": 1000, "unit": "W/K"}, "cold_capacity": {"value": 1000, "unit": "W/K"}, "UA": {"value": 1000, "unit": "W/K"}}


def cmd(app, rev, action, params):
    return ok(app, "session.command", session_id="s1", expected_revision=rev, action=action, params=params)


def setup(app):
    ok(app, "session.create", module_id="heat-exchanger")
    cmd(app, 0, "set_conditions", conditions())
    return cmd(app, 1, "capture_snapshot", {})["snapshot"]


def put(app, rev, task="demo.power", n=46, unit="W", answer_id=None, snap="hx-s1"):
    args = {"session_id": "s1", "expected_revision": rev, "task_id": task, "context_ref": {"snapshot_id": snap, "snapshot_version": 1}, "value": {"kind": "quantity", "quantity_id": "demo_power", "value": n, "unit": unit}}
    if answer_id is not None:
        args["answer_id"] = answer_id
    return ok(app, "answer.put", **args)["answer"]


def check(app, rev, answer_id="answer-1"):
    return ok(app, "answer.check", session_id="s1", expected_revision=rev, answer_id=answer_id)["answer"]


def test_answer_versions_context_and_roundtrip(tmp_path):
    app = Application(tmp_path)
    snapshot = setup(app)
    answer = put(app, 2, n=46)
    assert answer["answer_version"] == 1 and answer["verification"]["status"] == "not_checked"
    assert check(app, 3)["verification"]["status"] == "does_not_match"
    updated = put(app, 4, n=0.047, unit="kW", answer_id="answer-1")
    assert updated["answer_version"] == 2 and updated["value"]["entered"]["unit"] == "kW" and updated["value"]["unit"] == "W"
    verified = check(app, 5)
    assert verified["verification"]["status"] == "matches"
    assert verified["verification"]["answer_version"] == 2 and verified["verification"]["context_ref"] == {"snapshot_id": "hx-s1", "snapshot_version": 1}
    assert verified["verification"]["reference_version"] == "1"
    cmd(app, 6, "set_conditions", conditions(81))
    state = ok(app, "session.state", session_id="s1")["state"]
    assert state["snapshots"][0] == snapshot
    assert state["answers"][0]["data_state"] == "stale"
    assert state["answers"][0]["verification"]["status"] == "matches"
    stale = check(app, 7)
    assert stale["verification"]["status"] == "not_checked" and "изменились" in stale["verification"]["reason"]
    assert [x["status"] for x in stale["checks"]] == ["does_not_match", "matches", "not_checked"]
    assert stale["checks"][0]["answer_value"]["value"] == 46
    assert stale["checks"][1]["answer_value"]["entered"]["unit"] == "kW"
    doc = ok(app, "session.save", session_id="s1", expected_revision=8)["document"]
    ok(app, "storage.write", document=doc, target="answers.json")
    restored = Application(tmp_path)
    loaded = ok(restored, "storage.read", target="answers.json")["document"]
    assert ok(restored, "session.restore", document=loaded)["state"] == doc["module_data"]
    assert ok(restored, "session.state", session_id="s1")["state"]["answers"][0] == stale


@pytest.mark.parametrize("task,reason", [("demo.no_reference", "эталон"), ("demo.no_tolerance", "допуск")])
def test_missing_basis_is_not_checked(tmp_path, task, reason):
    app = Application(tmp_path)
    setup(app)
    put(app, 2, task=task)
    result = check(app, 3)
    assert result["verification"]["status"] == "not_checked" and reason in result["verification"]["reason"]


def test_invalid_answer_and_revision_do_not_change_state(tmp_path):
    app = Application(tmp_path)
    setup(app)
    put(app, 2)
    before = ok(app, "session.state", session_id="s1")
    base = {"session_id": "s1", "expected_revision": 3, "task_id": "demo.power", "answer_id": "answer-1", "context_ref": {"snapshot_id": "hx-s1", "snapshot_version": 1}, "value": {"kind": "quantity", "quantity_id": "demo_power", "value": 46, "unit": "kg"}}
    err = failure(app, "unit.incompatible", "answer.put", **base)
    assert err["diagnostics"][0]["field"] == "value.unit"
    bad = copy.deepcopy(base)
    bad["value"] = {"kind": "quantity", "quantity_id": "demo_power", "value": "abc", "unit": "W"}
    failure(app, "input.invalid_number", "answer.put", **bad)
    bad["expected_revision"] = 2
    failure(app, "revision.conflict", "answer.put", **bad)
    assert ok(app, "session.state", session_id="s1") == before


def test_no_accidental_reference_leak(tmp_path):
    app = Application(tmp_path)
    setup(app)
    put(app, 2, n=10)
    checked = check(app, 3)
    assert checked["verification"]["status"] == "does_not_match"
    outputs = [ok(app, "session.state", session_id="s1"), ok(app, "material.get", material_id="hx.fixture"), ok(app, "material.get", material_id="demo.answer"), ok(app, "session.save", session_id="s1", expected_revision=4), checked]
    assert all('47' not in json.dumps(x, ensure_ascii=False) for x in outputs)
    # The original student's answer remains visible; this is not a secrecy mechanism.
    assert checked["value"]["entered"]["value"] == 10


def test_r04_read_does_not_mutate_and_action_tracks_matching_snapshots(tmp_path):
    app = Application(tmp_path)
    ok(app, "session.create", module_id="heat-exchanger")
    cmd(app, 0, "set_conditions", conditions(81))
    cmd(app, 1, "capture_snapshot", {})
    action = next(a for a in ok(app, "session.actions", session_id="s1")["actions"] if a["action"] == "show_fixture")
    assert action["available"] is False
    cmd(app, 2, "set_conditions", conditions())
    cmd(app, 3, "capture_snapshot", {})
    before = ok(app, "session.state", session_id="s1")
    assert next(a for a in ok(app, "session.actions", session_id="s1")["actions"] if a["action"] == "show_fixture")["available"] is True
    assert cmd(app, 4, "show_fixture", {"snapshot_id": "hx-s2"})["revision"] == 4
    assert ok(app, "session.state", session_id="s1") == before
    failure(app, "fixture.no_match", "session.command", session_id="s1", expected_revision=4, action="show_fixture", params={"snapshot_id": "hx-s1"})


def test_d1_document_migration_is_conservative(tmp_path):
    app = Application(tmp_path)
    snap = setup(app)
    doc = ok(app, "session.save", session_id="s1", expected_revision=2)["document"]
    doc["module_data_version"] = 1
    data = doc["module_data"]
    del data["answers"], data["conditions_version"]
    for snapshot in data["snapshots"]:
        del snapshot["conditions_version"]
    new_app = Application(tmp_path)
    restored = ok(new_app, "session.restore", document=doc)["state"]
    assert restored["snapshots"][0]["conditions"] == snap["conditions"]
    assert restored["snapshots"][0]["conditions_version"] == -1
    answer = put(new_app, 2)
    assert answer["data_state"] == "stale"
    assert check(new_app, 3)["verification"]["status"] == "not_checked"
    assert ok(new_app, "session.save", session_id="s1", expected_revision=4)["document"]["module_data_version"] == 2


def test_cli_parity_for_all_answer_scenarios(tmp_path):
    ctx = {"snapshot_id": "hx-s1", "snapshot_version": 1}
    val = lambda n, unit="W": {"kind": "quantity", "quantity_id": "demo_power", "value": n, "unit": unit}
    requests = [request("session.create", module_id="heat-exchanger"), request("session.command", session_id="s1", expected_revision=0, action="set_conditions", params=conditions()), request("session.command", session_id="s1", expected_revision=1, action="capture_snapshot", params={}), request("answer.put", session_id="s1", expected_revision=2, task_id="demo.power", context_ref=ctx, value=val(10)), request("answer.check", session_id="s1", expected_revision=3, answer_id="answer-1"), request("answer.put", session_id="s1", expected_revision=4, answer_id="answer-1", task_id="demo.power", context_ref=ctx, value=val(0.047, "kW")), request("answer.check", session_id="s1", expected_revision=5, answer_id="answer-1"), request("answer.put", session_id="s1", expected_revision=6, task_id="demo.no_reference", context_ref=ctx, value=val(1)), request("answer.check", session_id="s1", expected_revision=7, answer_id="answer-2"), request("answer.put", session_id="s1", expected_revision=8, task_id="demo.no_tolerance", context_ref=ctx, value=val(1)), request("answer.check", session_id="s1", expected_revision=9, answer_id="answer-3"), request("answer.put", session_id="s1", expected_revision=10, task_id="demo.power", context_ref=ctx, value=val(1, "kg")), request("answer.put", session_id="s1", expected_revision=10, task_id="demo.power", context_ref=ctx, value=val("abc")), request("answer.check", session_id="s1", expected_revision=1, answer_id="answer-1"), request("session.command", session_id="s1", expected_revision=10, action="set_conditions", params=conditions(81)), request("answer.check", session_id="s1", expected_revision=11, answer_id="answer-1"), request("session.save", session_id="s1", expected_revision=12)]
    app = Application(tmp_path)
    expected = [app.request(r) for r in requests]
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    process = subprocess.run([sys.executable, "-m", "food_processes", "--storage-dir", str(tmp_path)], input=payload, encoding="utf-8", capture_output=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"}, check=True)
    actual = [json.loads(line) for line in process.stdout.splitlines()]
    assert actual == expected
    assert "47" not in json.dumps(actual[:5], ensure_ascii=False)
    assert actual[4]["data"]["answer"]["verification"]["status"] == "does_not_match"
    assert actual[6]["data"]["answer"]["verification"]["status"] == "matches"
    assert all(actual[i]["data"]["answer"]["verification"]["status"] == "not_checked" for i in (8, 10, 15))
    assert [actual[i]["diagnostics"][0]["code"] for i in (11, 12, 13)] == ["unit.incompatible", "input.invalid_number", "revision.conflict"]

def test_cli_saves_and_restores_answer_across_processes(tmp_path):
    setup_app = Application(tmp_path)
    setup(setup_app)
    put(setup_app, 2, n=10)
    check(setup_app, 3)
    document = ok(setup_app, "session.save", session_id="s1", expected_revision=4)["document"]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    first = [request("storage.write", document=document, target="answer.json"), request("storage.read", target="answer.json")]
    p1 = subprocess.run([sys.executable, "-m", "food_processes", "--storage-dir", str(tmp_path)], input="\n".join(json.dumps(r) for r in first) + "\n", encoding="utf-8", capture_output=True, env=env, check=True)
    output = [json.loads(line) for line in p1.stdout.splitlines()]
    assert all(x["outcome"] == "ok" for x in output)
    restored_doc = output[1]["data"]["document"]
    second = [request("session.restore", document=restored_doc), request("session.state", session_id="s1")]
    p2 = subprocess.run([sys.executable, "-m", "food_processes", "--storage-dir", str(tmp_path)], input="\n".join(json.dumps(r) for r in second) + "\n", encoding="utf-8", capture_output=True, env=env, check=True)
    restored = [json.loads(line) for line in p2.stdout.splitlines()]
    assert restored[1]["data"]["state"]["answers"][0]["verification"]["status"] == "does_not_match"
    assert restored[1]["data"]["state"]["answers"][0]["context_ref"] == {"snapshot_id": "hx-s1", "snapshot_version": 1}

def test_changing_back_does_not_revive_old_check_and_tampering_is_rejected(tmp_path):
    app = Application(tmp_path)
    setup(app)
    put(app, 2, n=10)
    check(app, 3)
    cmd(app, 4, "set_conditions", conditions(81))
    cmd(app, 5, "set_conditions", conditions(80))
    assert check(app, 6)["verification"]["status"] == "not_checked"
    doc = ok(app, "session.save", session_id="s1", expected_revision=7)["document"]
    forged = copy.deepcopy(doc)
    forged["module_data"]["answers"][0]["checks"][0]["status"] = "matches"
    other = Application(tmp_path)
    failure(other, "document.invalid", "session.restore", document=forged)
    assert ok(other, "session.restore", document=doc)["state"] == doc["module_data"]

def test_invalid_context_and_rebinding_existing_answer_are_rejected(tmp_path):
    app = Application(tmp_path)
    setup(app)
    put(app, 2, n=10)
    before = ok(app, "session.state", session_id="s1")
    base = {"session_id": "s1", "expected_revision": 3, "task_id": "demo.power", "context_ref": {"snapshot_id": "hx-s1", "snapshot_version": 2}, "value": {"kind": "quantity", "quantity_id": "demo_power", "value": 10, "unit": "W"}}
    failure(app, "answer.context_not_found", "answer.put", **base)
    base["context_ref"]["snapshot_version"] = 1
    base["answer_id"] = "answer-1"
    base["task_id"] = "demo.no_reference"
    failure(app, "answer.context_mismatch", "answer.put", **base)
    assert ok(app, "session.state", session_id="s1") == before
