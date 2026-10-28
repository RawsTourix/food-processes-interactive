"""Диспетчер, версии сеансов и ограниченное файловое хранилище."""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import uuid

from .modules import Air, Heat, MATERIALS, MODULES, Problem, fail, fixture_matches
from . import answers, storage


VERSION = "0.1"


def finite_tree(value):
    if isinstance(value, float) and not math.isfinite(value):
        return False
    if isinstance(value, dict):
        return all(isinstance(k, str) and finite_tree(v) for k, v in value.items())
    if isinstance(value, list):
        return all(finite_tree(v) for v in value)
    return value is None or isinstance(value, (str, int, float, bool))


def default_storage():
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "FoodProcessesInteractive" / "sessions" if base else Path.home() / ".local" / "share" / "food-processes-interactive" / "sessions"


def validate_document(doc):
    if not isinstance(doc, dict) or set(doc) != {"format", "format_version", "module_id", "module_version", "session_id", "revision", "module_data_version", "module_data"} or not finite_tree(doc):
        fail("document.invalid", "compatibility", "Формат документа повреждён.")
    if doc["format"] != "food-processes-session" or type(doc["format_version"]) is not int or doc["format_version"] != 1 or type(doc["module_data_version"]) is not int or doc["module_data_version"] not in (1, 2) or doc["module_version"] != "0.1":
        fail("document.unsupported", "compatibility", "Версия документа не поддерживается.")
    if not isinstance(doc["module_id"], str) or doc["module_id"] not in MODULES or not isinstance(doc["session_id"], str) or not re.fullmatch(r"(?:s[1-9][0-9]*|s-[0-9a-f]{32})", doc["session_id"]) or type(doc["revision"]) is not int or doc["revision"] < 0:
        fail("document.invalid", "compatibility", "Идентификатор модуля, сеанса или ревизия некорректны.")
    doc = deepcopy(doc)
    data = doc["module_data"]
    if not isinstance(data, dict):
        fail("document.invalid", "compatibility", "Данные модуля повреждены.")
    try:
        if doc["module_id"] == "heat-exchanger":
            if doc["module_data_version"] == 1:
                if set(data) != {"conditions", "drafts", "snapshots"} or not isinstance(data["snapshots"], list):
                    raise ValueError()
                data["conditions_version"] = 0
                data["answers"] = []
                for snap in data["snapshots"]:
                    if not isinstance(snap, dict) or set(snap) != {"snapshot_id", "snapshot_version", "data_state", "origin", "conditions"}:
                        raise ValueError()
                    snap["conditions_version"] = -1
                doc["module_data_version"] = 2
            if set(data) != {"conditions", "drafts", "snapshots", "conditions_version", "answers"} or type(data["conditions_version"]) is not int or data["conditions_version"] < 0 or not isinstance(data["answers"], list) or not isinstance(data["drafts"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data["drafts"].items()) or not isinstance(data["snapshots"], list):
                raise ValueError()
            def check_conditions(c):
                if not isinstance(c, dict) or set(c) != {"flow", "hot_in", "cold_in", "hot_capacity", "cold_capacity", "UA"}:
                    raise ValueError()
                raw = {"flow": c["flow"]}
                for k in ("hot_in", "cold_in", "hot_capacity", "cold_capacity", "UA"):
                    raw[k] = c[k]["entered"]
                tmp = Heat.initial()
                Heat.command(tmp, "set_conditions", raw)
                if tmp["conditions"] != c:
                    raise ValueError()
            if data["conditions"] is not None:
                check_conditions(data["conditions"])
            for i, snap in enumerate(data["snapshots"], 1):
                if set(snap) != {"snapshot_id", "snapshot_version", "data_state", "origin", "conditions", "conditions_version"} or type(snap["conditions_version"]) is not int or not -1 <= snap["conditions_version"] <= data["conditions_version"] or snap["snapshot_id"] != f"hx-s{i}" or type(snap["snapshot_version"]) is not int or snap["snapshot_version"] != 1 or snap["data_state"] != "frozen" or snap["origin"] != "user_entered":
                    raise ValueError()
                check_conditions(snap["conditions"])
            answers.validate_saved(data)
        else:
            if doc["module_data_version"] != 1:
                raise ValueError()
            if set(data) != {"selection"}:
                raise ValueError()
            selection = data["selection"]
            if selection is not None:
                tmp = Air.initial()
                Air.command(tmp, "select_region", {"point_id": selection["point_id"], "region_id": selection["region_id"]})
                if tmp["selection"] != selection:
                    raise ValueError()
    except (ValueError, KeyError, TypeError, Problem):
        fail("document.invalid", "compatibility", "Содержимое сеанса повреждено или несовместимо.")
    return deepcopy(doc)


class Application:
    """Обрабатывает те же запросы, что JSON Lines CLI. storage_dir задаётся доверенным вызывающим кодом."""

    def __init__(self, storage_dir=None):
        self.storage_dir = Path(storage_dir) if storage_dir is not None else default_storage()
        self.sessions = {}

    def _target(self, target):
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}\.json", target, flags=re.ASCII) or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])\.JSON", target.upper()):
            fail("storage.invalid_target", "storage", "Укажите имя файла JSON без пути.", field="target")
        root = self.storage_dir.resolve()
        path = root / target
        if path.is_symlink():
            fail("storage.invalid_target", "storage", "Символическая ссылка вместо файла не поддерживается.", field="target")
        return root, path

    def _write(self, document, target, expected):
        doc = validate_document(document)
        root, path = self._target(target)
        try:
            return storage.write_file(root, path, doc, expected, validate_document)
        except OSError:
            fail("storage.write_failed", "storage", "Не удалось подтвердить запись. Прочитайте файл перед повторной попыткой.")

    def _read(self, target):
        root, path = self._target(target)
        try:
            return storage.read_file(root, path, validate_document)
        except OSError:
            fail("storage.read_failed", "storage", "Не удалось прочитать файл.")

    def _session(self, args):
        sid = args.get("session_id")
        if not isinstance(sid, str) or sid not in self.sessions:
            fail("session.not_found", "input", "Сеанс не найден.", field="session_id")
        return self.sessions[sid]

    def _revision(self, args, session):
        expected = args.get("expected_revision")
        if type(expected) is not int:
            fail("input.required_revision", "input", "Укажите целую ожидаемую ревизию.", field="expected_revision")
        if expected != session["revision"]:
            fail("revision.conflict", "conflict", "Сеанс изменился; обновите состояние.", expected=expected, actual=session["revision"])

    def _dispatch(self, op, args):
        if op == "module.info":
            mid = args.get("module_id")
            if not isinstance(mid, str) or mid not in MODULES:
                fail("module.not_found", "input", "Учебный модуль не найден.")
            return deepcopy(MODULES[mid].info)
        if op == "material.get":
            mid = args.get("material_id")
            if not isinstance(mid, str) or mid not in MATERIALS:
                fail("material.not_found", "input", "Материал не найден.")
            return deepcopy(MATERIALS[mid])
        if op == "session.create":
            mid = args.get("module_id")
            if not isinstance(mid, str) or mid not in MODULES:
                fail("module.not_found", "input", "Учебный модуль не найден.")
            sid = "s-" + uuid.uuid4().hex
            while sid in self.sessions:
                sid = "s-" + uuid.uuid4().hex
            self.sessions[sid] = {"module_id": mid, "revision": 0, "state": MODULES[mid].initial()}
            return {"session_id": sid, "revision": 0, "state": deepcopy(self.sessions[sid]["state"]), "models": deepcopy(MODULES[mid].info["models"])}
        if op == "session.restore":
            doc = validate_document(args.get("document"))
            sid = doc["session_id"]
            if sid in self.sessions:
                fail("session.already_exists", "conflict", "Сеанс уже открыт; восстановление не выполнено.")
            self.sessions[sid] = {"module_id": doc["module_id"], "revision": doc["revision"], "state": doc["module_data"]}
            return {"session_id": sid, "revision": doc["revision"], "state": deepcopy(doc["module_data"]), "models": deepcopy(MODULES[doc["module_id"]].info["models"])}
        if op == "storage.write":
            if "expected_generation" not in args:
                fail("storage.generation_required", "input", "Укажите ожидаемый токен поколения или null для нового файла.", field="expected_generation")
            return self._write(args.get("document"), args.get("target"), args["expected_generation"])
        if op == "storage.read":
            return self._read(args.get("target"))
        if op in ("session.state", "session.actions", "session.command", "session.save", "answer.put", "answer.check"):
            session = self._session(args)
            mod = MODULES[session["module_id"]]
            if op == "session.state":
                return {"revision": session["revision"], "state": deepcopy(session["state"]), "models": deepcopy(mod.info["models"])}
            if op == "session.actions":
                actions = []
                for action in mod.actions:
                    reason = None
                    if action in ("get_stand_readings", "get_properties", "construct_process"):
                        reason = "Модель не утверждена."
                    elif action == "capture_snapshot" and session["state"]["conditions"] is None:
                        reason = "Сначала задайте условия."
                    elif action == "show_fixture" and not any(fixture_matches(s) for s in session["state"]["snapshots"]):
                        reason = "Нет снимка с условиями R04."
                    actions.append({"action": action, "available": reason is None, **({"reason": reason} if reason else {})})
                return {"revision": session["revision"], "actions": actions}
            self._revision(args, session)
            if op == "session.save":
                return {"saved_revision": session["revision"], "document": {"format": "food-processes-session", "format_version": 1, "module_id": session["module_id"], "module_version": "0.1", "session_id": args["session_id"], "revision": session["revision"], "module_data_version": 2 if mod is Heat else 1, "module_data": deepcopy(session["state"])}}
            if op in ("answer.put", "answer.check"):
                if mod is not Heat:
                    fail("answer.unsupported", "availability", "Для этого модуля проверка ответов пока недоступна.")
                new_state = deepcopy(session["state"])
                answer = answers.put(new_state, args) if op == "answer.put" else answers.check(new_state, args)
                session["state"] = new_state
                session["revision"] += 1
                return {"revision": session["revision"], "answer": answer}
            action, params = args.get("action"), args.get("params")
            if not isinstance(action, str) or not isinstance(params, dict):
                fail("input.invalid_command", "input", "Укажите команду и объект параметров.")
            new_state = deepcopy(session["state"])
            result = mod.command(new_state, action, params)
            if mod is Heat and action == "show_fixture":
                return {"revision": session["revision"], **result}
            session["state"] = new_state
            session["revision"] += 1
            return {"revision": session["revision"], **result}
        fail("operation.unknown", "input", "Неизвестная операция.", field="op")

    def request(self, request):
        request_id = request.get("request_id") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict) or not isinstance(request_id, str) or not isinstance(request.get("op"), str) or not isinstance(request.get("args"), dict):
                fail("request.invalid", "input", "Ожидается запрос с request_id, op и объектом args.")
            if request.get("api_version") != VERSION:
                fail("api.unsupported_version", "compatibility", "Версия API не поддерживается.")
            if not finite_tree(request):
                fail("input.nonfinite", "input", "Запрос содержит недопустимое значение JSON.")
            data = self._dispatch(request["op"], request["args"])
            return {"api_version": VERSION, "request_id": request_id, "outcome": "ok", "data": data, "diagnostics": []}
        except Problem as error:
            return {"api_version": VERSION, "request_id": request_id, "outcome": "error", "data": None, "diagnostics": [error.diagnostic]}
