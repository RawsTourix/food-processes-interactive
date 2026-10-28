"""Предметные команды Д1. Числа R04 — открытая демонстрационная фикстура."""
from copy import deepcopy
import math


class Problem(Exception):
    def __init__(self, code, category, message, **details):
        self.diagnostic = {"code": code, "category": category, "severity": "error", "message": message, **details}
        super().__init__(message)


MATERIALS = {
    "hx.fixture": {"material_id": "hx.fixture", "title": "Условный пример R04", "text": "Результат является фиксированным учебным примером, а не измерением установки.", "source": "docs/design/heat-exchanger-reference-cases.md#эталонные-случаи"},
    "demo.answer": {"material_id": "demo.answer", "title": "Демонстрационное задание", "text": "Введите условную мощность в W или kW. Проверка демонстрирует механизм ответов и не является физическим расчётом или заданием, согласованным преподавателем.", "source": "docs/design/headless-d2a.md"},
    "air.region": {"material_id": "air.region", "title": "Качественная область", "text": "Это условная область схемы без физических координат и свойств.", "source": "docs/design/laboratory-portfolio.md"},
}


def fail(code, category, message, **details):
    raise Problem(code, category, message, **details)


def quantity(params, key, kind):
    field = f"params.{key}"
    item = params.get(key)
    if not isinstance(item, dict):
        fail("input.required", "input", "Укажите значение и единицу.", field=field)
    value, unit = item.get("value"), item.get("unit")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail("input.invalid_number", "input", "Введите конечное число.", field=field + ".value")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        fail("input.invalid_number", "input", "Введите конечное число.", field=field + ".value")
    allowed = {"temperature": ("degC", "K"), "capacity": ("W/K", "kW/K")}[kind]
    if unit not in allowed:
        fail("unit.incompatible", "input", "Единица не соответствует величине.", field=field + ".unit", expected={"units": list(allowed)}, actual=unit)
    # Absolute Kelvin is converted with an offset; W/K describes a temperature difference.
    normalized = value - 273.15 if kind == "temperature" and unit == "K" else value * 1000 if unit == "kW/K" else value
    try:
        finite_normalized = math.isfinite(normalized)
    except OverflowError:
        finite_normalized = False
    if not finite_normalized:
        fail("input.invalid_number", "input", "Результат преобразования должен быть конечным.", field=field + ".value")
    if kind == "temperature" and normalized < -273.15:
        fail("input.out_of_range", "input", "Абсолютная температура не может быть ниже нуля кельвинов.", field=field + ".value")
    if kind == "capacity" and (normalized < 0 or (key != "UA" and normalized == 0)):
        fail("applicability.invalid_capacity", "applicability", "Теплоёмкостный расход должен быть положительным, UA — неотрицательным.", field=field + ".value")
    return {"value": normalized, "unit": "degC" if kind == "temperature" else "W/K", "entered": {"value": value, "unit": unit}}


def fixture_matches(snapshot):
    c = snapshot["conditions"]
    return c["flow"] == "counter" and all(c[k]["value"] == v for k, v in {"hot_in": 80, "cold_in": 20, "hot_capacity": 1000, "cold_capacity": 1000, "UA": 1000}.items())


class Heat:
    info = {"module_id": "heat-exchanger", "title": "Теплообменник", "module_version": "0.1", "interaction_types": ["calculation"], "models": [{"capability": "r04_fixture", "availability": "available", "basis": "учебная фикстура R04"}, {"capability": "stand_readings", "availability": "unavailable", "basis": "модель установки не утверждена"}]}
    actions = ["set_conditions", "save_draft", "capture_snapshot", "show_fixture", "get_stand_readings"]

    @staticmethod
    def initial():
        return {"conditions": None, "drafts": {}, "snapshots": [], "conditions_version": 0, "answers": []}

    @staticmethod
    def command(state, action, params):
        if action == "get_stand_readings":
            fail("model.stand_unavailable", "availability", "Показания стенда недоступны: модель не подтверждена.")
        if action == "save_draft":
            field, text = params.get("field"), params.get("text")
            if not isinstance(field, str) or not isinstance(text, str) or not field:
                fail("input.invalid_draft", "input", "Укажите поле и текст черновика.")
            state["drafts"][field] = text
            return {"draft": {"field": field, "text": text}}
        if action == "set_conditions":
            flow = params.get("flow")
            if flow not in ("counter", "parallel"):
                fail("input.invalid_flow", "input", "Выберите прямоток или противоток.", field="params.flow")
            conditions = {"flow": flow}
            for key in ("hot_in", "cold_in"):
                conditions[key] = quantity(params, key, "temperature")
            for key in ("hot_capacity", "cold_capacity", "UA"):
                conditions[key] = quantity(params, key, "capacity")
            state["conditions"] = conditions
            state["conditions_version"] += 1
            for answer in state["answers"]:
                answer["data_state"] = "stale"
            return {"conditions": deepcopy(conditions)}
        if action == "capture_snapshot":
            if state["conditions"] is None:
                fail("input.missing_conditions", "input", "Сначала задайте условия.")
            snapshot = {"snapshot_id": f"hx-s{len(state['snapshots']) + 1}", "snapshot_version": 1, "data_state": "frozen", "origin": "user_entered", "conditions_version": state["conditions_version"], "conditions": deepcopy(state["conditions"])}
            state["snapshots"].append(snapshot)
            return {"snapshot": deepcopy(snapshot)}
        if action == "show_fixture":
            snapshot = next((s for s in state["snapshots"] if s["snapshot_id"] == params.get("snapshot_id")), None)
            if snapshot is None:
                fail("input.snapshot_not_found", "input", "Снимок не найден.", field="params.snapshot_id")
            if not fixture_matches(snapshot):
                fail("fixture.no_match", "availability", "Фикстура R04 доступна только для соответствующих условий; расчёт не реализован.")
            return {"fixture": {"kind": "demonstration_fixture", "source_id": "hx-r04", "context_ref": {"snapshot_id": snapshot["snapshot_id"]}, "message": "Открытая учебная фикстура R04; это не расчёт и не показания установки.", "values": {"heat_rate": {"value": 30000, "unit": "W"}, "hot_out": {"value": 50, "unit": "degC"}, "cold_out": {"value": 50, "unit": "degC"}}}}
        fail("action.unknown", "input", "Неизвестная команда.", field="action")


class Air:
    info = {"module_id": "humid-air", "title": "Влажный воздух", "module_version": "0.1", "interaction_types": ["qualitative_diagram"], "models": [{"capability": "psychrometric_properties", "availability": "unavailable", "basis": "модель и давление не утверждены"}]}
    actions = ["select_region", "get_properties", "construct_process"]

    @staticmethod
    def initial():
        return {"selection": None}

    @staticmethod
    def command(state, action, params):
        if action in ("get_properties", "construct_process"):
            fail("model.psychrometrics_unavailable", "availability", "Численные свойства и физическое построение недоступны: модель не утверждена.")
        if action == "select_region":
            region, point = params.get("region_id"), params.get("point_id")
            if region != "schematic-area-a" or not isinstance(point, str) or not point:
                fail("input.invalid_selection", "input", "Выберите доступную качественную область и укажите ID точки.")
            state["selection"] = {"kind": "diagram_point", "point_id": point, "representation": "qualitative", "region_id": region, "material_id": "air.region"}
            return {"selection": deepcopy(state["selection"]), "data_state": "draft"}
        fail("action.unknown", "input", "Неизвестная команда.", field="action")


MODULES = {"heat-exchanger": Heat, "humid-air": Air}
