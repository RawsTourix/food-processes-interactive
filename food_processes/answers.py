"""Демонстрационная проверка ответов, не утверждённая учебная методика."""
from copy import deepcopy
import math
from .modules import fail

# A local offline educational policy, not protection against inspecting source code.
_TASKS = {
    "demo.power": (47.0, 0.5),
    "demo.no_reference": (None, 0.5),
    "demo.no_tolerance": (47.0, None),
}


def normalize(value):
    if not isinstance(value, dict) or value.get("kind") != "quantity" or value.get("quantity_id") != "demo_power":
        fail("answer.invalid_type", "input", "Укажите числовой ответ демонстрационного задания.", field="value")
    number, unit = value.get("value"), value.get("unit")
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        fail("input.invalid_number", "input", "Введите конечное число.", field="value.value")
    try:
        finite = math.isfinite(number)
    except OverflowError:
        finite = False
    if not finite:
        fail("input.invalid_number", "input", "Введите конечное число.", field="value.value")
    if unit not in ("W", "kW"):
        fail("unit.incompatible", "input", "Единица не соответствует мощности.", field="value.unit", expected={"units": ["W", "kW"]})
    normalized = number * (1000 if unit == "kW" else 1)
    try:
        finite = math.isfinite(normalized)
    except OverflowError:
        finite = False
    if not finite:
        fail("input.invalid_number", "input", "Значение после преобразования должно быть конечным.", field="value.value")
    return {"kind": "quantity", "quantity_id": "demo_power", "value": normalized, "unit": "W", "entered": {"value": number, "unit": unit}}


def put(state, args):
    task = args.get("task_id")
    if not isinstance(task, str) or task not in _TASKS:
        fail("task.not_found", "input", "Демонстрационное задание не найдено.", field="task_id")
    context = args.get("context_ref")
    if not isinstance(context, dict) or set(context) != {"snapshot_id", "snapshot_version"} or type(context.get("snapshot_version")) is not int:
        fail("answer.invalid_context", "input", "Укажите идентификатор и версию снимка.", field="context_ref")
    snap = next((s for s in state["snapshots"] if s["snapshot_id"] == context["snapshot_id"] and s["snapshot_version"] == context["snapshot_version"]), None)
    if snap is None:
        fail("answer.context_not_found", "input", "Снимок указанной версии не найден.", field="context_ref")
    value = normalize(args.get("value"))
    aid = args.get("answer_id")
    answer = None
    if aid is not None:
        answer = next((a for a in state["answers"] if a["answer_id"] == aid), None)
        if answer is None:
            fail("answer.not_found", "input", "Ответ не найден.", field="answer_id")
        if answer["task_id"] != task or answer["context_ref"] != context:
            fail("answer.context_mismatch", "input", "Задание и снимок существующего ответа менять нельзя.")
    if answer is None:
        answer = {"answer_id": f"answer-{len(state['answers']) + 1}", "task_id": task, "context_ref": deepcopy(context), "answer_version": 0, "checks": []}
        state["answers"].append(answer)
    answer["answer_version"] += 1
    answer["value"] = value
    answer["data_state"] = "current" if snap["conditions_version"] == state["conditions_version"] and snap["conditions_version"] > 0 else "stale"
    answer["verification"] = {"status": "not_checked", "reason": "Ответ ещё не проверен."}
    return deepcopy(answer)


def check(state, args):
    aid = args.get("answer_id")
    answer = next((a for a in state["answers"] if a["answer_id"] == aid), None)
    if answer is None:
        fail("answer.not_found", "input", "Ответ не найден.", field="answer_id")
    reference, tolerance = _TASKS[answer["task_id"]]
    if answer["data_state"] == "stale":
        verification = {"status": "not_checked", "reason": "Исходные условия изменились или версия старого снимка неизвестна."}
    elif reference is None:
        verification = {"status": "not_checked", "reason": "Для задания не задан эталон."}
    elif tolerance is None:
        verification = {"status": "not_checked", "reason": "Для задания не задан допуск."}
    else:
        status = "matches" if abs(answer["value"]["value"] - reference) <= tolerance else "does_not_match"
        verification = {"status": status, "method_id": "demo.absolute_difference", "reference_id": "demo.reference", "reference_version": "1", "tolerance": {"value": tolerance, "unit": "W"}}
    verification.update({"answer_version": answer["answer_version"], "answer_value": deepcopy(answer["value"]), "context_ref": deepcopy(answer["context_ref"])})
    answer["verification"] = verification
    answer["checks"].append(deepcopy(verification))
    return deepcopy(answer)


def validate_saved(state):
    """Validate persisted answer structure and its links before restoring a session."""
    for index, answer in enumerate(state["answers"], 1):
        if not isinstance(answer, dict) or set(answer) != {"answer_id", "task_id", "context_ref", "answer_version", "checks", "value", "data_state", "verification"}:
            raise ValueError()
        if answer["answer_id"] != f"answer-{index}" or answer["task_id"] not in _TASKS or type(answer["answer_version"]) is not int or answer["answer_version"] < 1:
            raise ValueError()
        context = answer["context_ref"]
        if not isinstance(context, dict) or set(context) != {"snapshot_id", "snapshot_version"} or type(context["snapshot_version"]) is not int:
            raise ValueError()
        snap = next((s for s in state["snapshots"] if s["snapshot_id"] == context["snapshot_id"] and s["snapshot_version"] == context["snapshot_version"]), None)
        if snap is None:
            raise ValueError()
        expected_state = "current" if snap["conditions_version"] == state["conditions_version"] and snap["conditions_version"] > 0 else "stale"
        if answer["data_state"] != expected_state:
            raise ValueError()
        value = answer["value"]
        if not isinstance(value, dict) or set(value) != {"kind", "quantity_id", "value", "unit", "entered"} or not isinstance(value["entered"], dict) or set(value["entered"]) != {"value", "unit"}:
            raise ValueError()
        if normalize({"kind": "quantity", "quantity_id": "demo_power", **value["entered"]}) != value:
            raise ValueError()
        if not isinstance(answer["checks"], list) or not isinstance(answer["verification"], dict):
            raise ValueError()
        values_by_version = {}
        for record in answer["checks"]:
            if not isinstance(record, dict) or record.get("context_ref") != context or type(record.get("answer_version")) is not int or not 1 <= record["answer_version"] <= answer["answer_version"]:
                raise ValueError()
            checked_value = record.get("answer_value")
            if not isinstance(checked_value, dict) or set(checked_value) != {"kind", "quantity_id", "value", "unit", "entered"} or not isinstance(checked_value["entered"], dict) or set(checked_value["entered"]) != {"value", "unit"} or normalize({"kind": "quantity", "quantity_id": "demo_power", **checked_value["entered"]}) != checked_value:
                raise ValueError()
            version = record["answer_version"]
            if version in values_by_version and values_by_version[version] != checked_value:
                raise ValueError()
            values_by_version[version] = checked_value
            if version == answer["answer_version"] and checked_value != value:
                raise ValueError()
            status = record.get("status")
            if status == "not_checked":
                if set(record) != {"status", "reason", "answer_version", "answer_value", "context_ref"} or not isinstance(record["reason"], str):
                    raise ValueError()
            elif status in ("matches", "does_not_match"):
                if set(record) != {"status", "method_id", "reference_id", "reference_version", "tolerance", "answer_version", "answer_value", "context_ref"} or record["method_id"] != "demo.absolute_difference" or record["reference_id"] != "demo.reference" or record["reference_version"] != "1" or record["tolerance"] != {"value": 0.5, "unit": "W"} or answer["task_id"] != "demo.power":
                    raise ValueError()
                expected_status = "matches" if abs(checked_value["value"] - _TASKS["demo.power"][0]) <= _TASKS["demo.power"][1] else "does_not_match"
                if status != expected_status:
                    raise ValueError()
            else:
                raise ValueError()
        initial = {"status": "not_checked", "reason": "Ответ ещё не проверен."}
        if answer["verification"] != initial:
            if not answer["checks"] or answer["verification"] != answer["checks"][-1] or answer["verification"]["answer_version"] != answer["answer_version"]:
                raise ValueError()
