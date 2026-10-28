import copy
import json
import os
import subprocess
import sys

import pytest
from food_processes import Application


def req(op, **args):
    return {"api_version": "0.1", "request_id": "test", "op": op, "args": args}


def call(app, op, **args):
    return app.request(req(op, **args))


def ok(app, op, **args):
    response = call(app, op, **args)
    assert response["outcome"] == "ok", response
    return response["data"]


def error(app, code, op, **args):
    response = call(app, op, **args)
    assert response["outcome"] == "error", response
    assert response["diagnostics"][0]["code"] == code
    return response["diagnostics"][0]


def command(app, sid, revision, action, **params):
    return ok(app, "session.command", session_id=sid, expected_revision=revision, action=action, params=params)


def conditions():
    return {"flow": "counter", "hot_in": {"value": 80, "unit": "degC"}, "cold_in": {"value": 20, "unit": "degC"}, "hot_capacity": {"value": 1000, "unit": "W/K"}, "cold_capacity": {"value": 1000, "unit": "W/K"}, "UA": {"value": 1000, "unit": "W/K"}}


def new(app, module):
    return ok(app, "session.create", module_id=module)["session_id"]


def save(app, sid, rev):
    return ok(app, "session.save", session_id=sid, expected_revision=rev)["document"]


def test_heat_fixture_snapshots_and_no_real_model(tmp_path):
    app = Application(tmp_path)
    sid = new(app, "heat-exchanger")
    actions = ok(app, "session.actions", session_id=sid)["actions"]
    assert next(a for a in actions if a["action"] == "get_stand_readings")["available"] is False
    command(app, sid, 0, "set_conditions", **conditions())
    first = command(app, sid, 1, "capture_snapshot")["snapshot"]
    fixture = command(app, sid, 2, "show_fixture", snapshot_id=first["snapshot_id"])["fixture"]
    assert fixture["kind"] == "demonstration_fixture"
    assert fixture["values"]["heat_rate"] == {"value": 30000, "unit": "W"}
    assert fixture["values"]["hot_out"]["value"] == fixture["values"]["cold_out"]["value"] == 50
    changed = conditions()
    changed["hot_in"]["value"] = 81
    command(app, sid, 3, "set_conditions", **changed)
    state = ok(app, "session.state", session_id=sid)
    assert state["state"]["snapshots"][0] == first
    assert state["state"]["conditions"]["hot_in"]["value"] == 81
    second = command(app, sid, 4, "capture_snapshot")["snapshot"]
    error(app, "fixture.no_match", "session.command", session_id=sid, expected_revision=5, action="show_fixture", params={"snapshot_id": second["snapshot_id"]})
    error(app, "model.stand_unavailable", "session.command", session_id=sid, expected_revision=5, action="get_stand_readings", params={})
    assert ok(app, "session.state", session_id=sid)["revision"] == 5


def test_air_is_qualitative_and_has_material(tmp_path):
    app = Application(tmp_path)
    sid = new(app, "humid-air")
    selection = command(app, sid, 0, "select_region", region_id="schematic-area-a", point_id="p1")["selection"]
    assert selection["representation"] == "qualitative"
    assert not {"x", "y", "unit", "temperature", "humidity"} & selection.keys()
    material = ok(app, "material.get", material_id=selection["material_id"])
    assert "физических координат" in material["text"]
    assert "30000" not in json.dumps(material, ensure_ascii=False)
    command(app, sid, 1, "select_region", region_id="schematic-area-a", point_id="p2")
    for action in ("get_properties", "construct_process"):
        error(app, "model.psychrometrics_unavailable", "session.command", session_id=sid, expected_revision=2, action=action, params={})
    assert ok(app, "session.state", session_id=sid)["state"]["selection"]["point_id"] == "p2"


def test_diagnostics_are_atomic_and_draft_survives(tmp_path):
    app = Application(tmp_path)
    sid = new(app, "heat-exchanger")
    command(app, sid, 0, "save_draft", field="hot_in", text="пока не число")
    before = ok(app, "session.state", session_id=sid)
    data = conditions()
    data["hot_in"]["value"] = "abc"
    error(app, "input.invalid_number", "session.command", session_id=sid, expected_revision=1, action="set_conditions", params=data)
    data = conditions()
    data["hot_in"]["unit"] = "kg"
    d = error(app, "unit.incompatible", "session.command", session_id=sid, expected_revision=1, action="set_conditions", params=data)
    assert d["field"] == "params.hot_in.unit"
    error(app, "revision.conflict", "session.command", session_id=sid, expected_revision=0, action="save_draft", params={"field": "x", "text": "lost"})
    error(app, "action.unknown", "session.command", session_id=sid, expected_revision=1, action="unknown", params={})
    assert ok(app, "session.state", session_id=sid) == before
    assert before["state"]["drafts"]["hot_in"] == "пока не число"


def test_unit_conversion_absolute_temperature(tmp_path):
    app = Application(tmp_path)
    sid = new(app, "heat-exchanger")
    values = conditions()
    values["hot_in"] = {"value": 353.15, "unit": "K"}
    values["hot_capacity"] = {"value": 1, "unit": "kW/K"}
    result = command(app, sid, 0, "set_conditions", **values)["conditions"]
    assert result["hot_in"]["value"] == 80
    assert result["hot_in"]["entered"]["unit"] == "K"
    assert result["hot_capacity"]["value"] == 1000
    assert result["hot_capacity"]["unit"] == "W/K"


@pytest.mark.parametrize("module", ["heat-exchanger", "humid-air"])
def test_roundtrip_and_incompatible_restore(tmp_path, module):
    app = Application(tmp_path)
    sid = new(app, module)
    if module == "heat-exchanger":
        command(app, sid, 0, "set_conditions", **conditions())
        command(app, sid, 1, "capture_snapshot")
        rev = 2
    else:
        command(app, sid, 0, "select_region", region_id="schematic-area-a", point_id="p1")
        rev = 1
    doc = save(app, sid, rev)
    ok(app, "storage.write", document=doc, target="session.json")
    read = ok(app, "storage.read", target="session.json")["document"]
    assert read == doc
    restored = Application(tmp_path)
    assert ok(restored, "session.restore", document=read)["state"] == doc["module_data"]
    error(restored, "session.already_exists", "session.restore", document=read)
    bad = copy.deepcopy(doc)
    bad["format_version"] = 2
    error(restored, "document.unsupported", "session.restore", document=bad)
    assert ok(restored, "session.state", session_id=sid)["state"] == doc["module_data"]


def test_failed_write_preserves_old_and_bad_read_keeps_session(tmp_path, monkeypatch):
    app = Application(tmp_path)
    sid = new(app, "humid-air")
    command(app, sid, 0, "select_region", region_id="schematic-area-a", point_id="p1")
    doc = save(app, sid, 1)
    ok(app, "storage.write", document=doc, target="session.json")
    old = (tmp_path / "session.json").read_bytes()
    command(app, sid, 1, "select_region", region_id="schematic-area-a", point_id="p2")
    new_doc = save(app, sid, 2)
    def broken(*args):
        raise OSError("simulated failure")
    monkeypatch.setattr(os, "replace", broken)
    error(app, "storage.write_failed", "storage.write", document=new_doc, target="session.json")
    assert (tmp_path / "session.json").read_bytes() == old
    assert not list(tmp_path.glob("*.tmp"))
    (tmp_path / "bad.json").write_text("{broken", encoding="utf-8")
    error(app, "storage.read_failed", "storage.read", target="bad.json")
    assert ok(app, "session.state", session_id=sid)["state"]["selection"]["point_id"] == "p2"


@pytest.mark.parametrize("target", ["../escape.json", "C:/escape.json", "/tmp/escape.json", "a/b.json", "file.txt", "..\\escape.json"])
def test_storage_rejects_paths(tmp_path, target):
    app = Application(tmp_path)
    sid = new(app, "humid-air")
    error(app, "storage.invalid_target", "storage.write", document=save(app, sid, 0), target=target)


def test_nonfinite_never_enters_state_or_serialization(tmp_path):
    app = Application(tmp_path)
    sid = new(app, "heat-exchanger")
    for invalid in (float("nan"), float("inf"), float("-inf")):
        data = conditions()
        data["hot_in"]["value"] = invalid
        error(app, "input.nonfinite", "session.command", session_id=sid, expected_revision=0, action="set_conditions", params=data)
    doc = save(app, sid, 0)
    json.dumps(doc, allow_nan=False)
    doc["module_data"]["drafts"]["x"] = float("nan")
    error(app, "input.nonfinite", "storage.write", document=doc, target="invalid.json")
    assert not (tmp_path / "invalid.json").exists()


def test_python_cli_parity_two_sessions(tmp_path):
    requests = [req("session.create", module_id="heat-exchanger"), req("session.command", session_id="s1", expected_revision=0, action="set_conditions", params=conditions()), req("session.command", session_id="s1", expected_revision=1, action="capture_snapshot", params={}), req("session.command", session_id="s1", expected_revision=2, action="show_fixture", params={"snapshot_id": "hx-s1"}), req("session.create", module_id="humid-air"), req("session.command", session_id="s2", expected_revision=0, action="select_region", params={"point_id": "p1", "region_id": "schematic-area-a"}), req("session.command", session_id="s2", expected_revision=1, action="get_properties", params={}), req("session.save", session_id="s2", expected_revision=1)]
    app = Application(tmp_path)
    python_results = [app.request(r) for r in requests]
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    process = subprocess.run([sys.executable, "-m", "food_processes", "--storage-dir", str(tmp_path)], input=text, encoding="utf-8", capture_output=True, env=env, check=True)
    cli_results = [json.loads(line) for line in process.stdout.splitlines()]
    assert cli_results == python_results
    assert all(r["outcome"] == "ok" for r in cli_results[:6])
    assert cli_results[6]["diagnostics"][0]["code"] == "model.psychrometrics_unavailable"


def test_cli_rejects_nonstandard_json_and_continues(tmp_path):
    good = json.dumps(req("module.info", module_id="humid-air"))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-m", "food_processes", "--storage-dir", str(tmp_path)], input='{"x":NaN}\n' + good + '\n', encoding="utf-8", capture_output=True, env=env, check=True)
    lines = [json.loads(x) for x in proc.stdout.splitlines()]
    assert lines[0]["diagnostics"][0]["code"] == "json.invalid"
    assert lines[1]["outcome"] == "ok"

def test_invalid_document_shape_does_not_replace_existing(tmp_path):
    app = Application(tmp_path)
    sid = new(app, "humid-air")
    bad = save(app, sid, 0)
    bad["module_id"] = []
    error(app, "document.invalid", "session.restore", document=bad)
    assert ok(app, "session.state", session_id=sid)["revision"] == 0


def test_enormous_number_returns_diagnostic(tmp_path):
    app = Application(tmp_path)
    sid = new(app, "heat-exchanger")
    data = conditions()
    data["hot_in"]["value"] = 10**1000
    error(app, "input.invalid_number", "session.command", session_id=sid, expected_revision=0, action="set_conditions", params=data)
    assert ok(app, "session.state", session_id=sid)["revision"] == 0


def test_target_symlink_rejected_when_available(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.json")
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Символические ссылки недоступны в этой среде")
    app = Application(tmp_path)
    sid = new(app, "humid-air")
    error(app, "storage.invalid_target", "storage.write", document=save(app, sid, 0), target="link.json")
    assert outside.read_text(encoding="utf-8") == "outside"


def test_unit_conversion_overflow_returns_diagnostic(tmp_path):
    app = Application(tmp_path)
    sid = new(app, "heat-exchanger")
    data = conditions()
    data["hot_capacity"] = {"value": 10**307, "unit": "kW/K"}
    error(app, "input.invalid_number", "session.command", session_id=sid, expected_revision=0, action="set_conditions", params=data)
    assert ok(app, "session.state", session_id=sid)["revision"] == 0

def test_materials_and_saved_state_do_not_include_fixture_answers(tmp_path):
    app = Application(tmp_path)
    sid = new(app, "heat-exchanger")
    command(app, sid, 0, "set_conditions", **conditions())
    command(app, sid, 1, "capture_snapshot")
    for material_id in ("hx.fixture", "air.region"):
        material = json.dumps(ok(app, "material.get", material_id=material_id), ensure_ascii=False)
        assert "30000" not in material
    state = json.dumps(ok(app, "session.state", session_id=sid), ensure_ascii=False)
    document = json.dumps(save(app, sid, 2), ensure_ascii=False)
    assert "30000" not in state and "30000" not in document
