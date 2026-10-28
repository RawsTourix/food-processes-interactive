"""Диспетчер, версии сеансов и ограниченное файловое хранилище."""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import tempfile

from .modules import Air, Heat, MATERIALS, MODULES, Problem, fail


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
    if doc["format"] != "food-processes-session" or type(doc["format_version"]) is not int or doc["format_version"] != 1 or doc["module_data_version"] != 1 or type(doc["module_data_version"]) is not int or doc["module_version"] != "0.1":
        fail("document.unsupported", "compatibility", "Версия документа не поддерживается.")
    if not isinstance(doc["module_id"], str) or doc["module_id"] not in MODULES or not isinstance(doc["session_id"], str) or not re.fullmatch(r"s[1-9][0-9]*", doc["session_id"]) or type(doc["revision"]) is not int or doc["revision"] < 0:
        fail("document.invalid", "compatibility", "Идентификатор модуля, сеанса или ревизия некорректны.")
    data = doc["module_data"]
    if not isinstance(data, dict):
        fail("document.invalid", "compatibility", "Данные модуля повреждены.")
    try:
        if doc["module_id"] == "heat-exchanger":
            if set(data) != {"conditions", "drafts", "snapshots"} or not isinstance(data["drafts"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data["drafts"].items()) or not isinstance(data["snapshots"], list):
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
                if set(snap) != {"snapshot_id", "snapshot_version", "data_state", "origin", "conditions"} or snap["snapshot_id"] != f"hx-s{i}" or type(snap["snapshot_version"]) is not int or snap["snapshot_version"] != 1 or snap["data_state"] != "frozen" or snap["origin"] != "user_entered":
                    raise ValueError()
                check_conditions(snap["conditions"])
        else:
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
        self.counter = 0

    def _target(self, target):
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}\.json", target, flags=re.ASCII):
            fail("storage.invalid_target", "storage", "Укажите имя файла JSON без пути.", field="target")
        root = self.storage_dir.resolve()
        path = root / target
        if path.is_symlink():
            fail("storage.invalid_target", "storage", "Символическая ссылка вместо файла не поддерживается.", field="target")
        return root, path

    def _write(self, document, target):
        doc = validate_document(document)
        root, path = self._target(target)
        temp = None
        try:
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=root, prefix=".session-", suffix=".tmp", delete=False) as f:
                temp = Path(f.name)
                json.dump(doc, f, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
            temp = None
        except OSError:
            fail("storage.write_failed", "storage", "Не удалось сохранить файл; повторите запись.")
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
        return {"target": target, "saved_revision": doc["revision"]}

    def _read(self, target):
        _, path = self._target(target)
        try:
            with path.open("r", encoding="utf-8") as f:
                doc = json.load(f, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (OSError, ValueError, UnicodeError):
            fail("storage.read_failed", "storage", "Не удалось прочитать корректный JSON-файл.")
        return {"document": validate_document(doc)}

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
            self.counter += 1
            sid = f"s{self.counter}"
            self.sessions[sid] = {"module_id": mid, "revision": 0, "state": MODULES[mid].initial()}
            return {"session_id": sid, "revision": 0, "state": deepcopy(self.sessions[sid]["state"]), "models": deepcopy(MODULES[mid].info["models"])}
        if op == "session.restore":
            doc = validate_document(args.get("document"))
            sid = doc["session_id"]
            if sid in self.sessions:
                fail("session.already_exists", "conflict", "Сеанс уже открыт; восстановление не выполнено.")
            self.sessions[sid] = {"module_id": doc["module_id"], "revision": doc["revision"], "state": doc["module_data"]}
            self.counter = max(self.counter, int(sid[1:]))
            return {"session_id": sid, "revision": doc["revision"], "state": deepcopy(doc["module_data"]), "models": deepcopy(MODULES[doc["module_id"]].info["models"])}
        if op == "storage.write":
            return self._write(args.get("document"), args.get("target"))
        if op == "storage.read":
            return self._read(args.get("target"))
        if op in ("session.state", "session.actions", "session.command", "session.save"):
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
                    elif action == "show_fixture" and not session["state"]["snapshots"]:
                        reason = "Сначала зафиксируйте снимок."
                    actions.append({"action": action, "available": reason is None, **({"reason": reason} if reason else {})})
                return {"revision": session["revision"], "actions": actions}
            self._revision(args, session)
            if op == "session.save":
                return {"saved_revision": session["revision"], "document": {"format": "food-processes-session", "format_version": 1, "module_id": session["module_id"], "module_version": "0.1", "session_id": args["session_id"], "revision": session["revision"], "module_data_version": 1, "module_data": deepcopy(session["state"])}}
            action, params = args.get("action"), args.get("params")
            if not isinstance(action, str) or not isinstance(params, dict):
                fail("input.invalid_command", "input", "Укажите команду и объект параметров.")
            new_state = deepcopy(session["state"])
            result = mod.command(new_state, action, params)
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
