"""Локальное хранение: блокировка процесса и контроль поколения файла."""
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from .modules import fail


@contextmanager
def file_lock(root, target, timeout=5.0):
    """Стабильный служебный файл не удаляется; блокировка снимается ОС при закрытии."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / ("." + target.lower() + ".lock")
    if path.is_symlink():
        fail("storage.invalid_target", "storage", "Символическая ссылка вместо файла блокировки не поддерживается.")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    locked = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    fail("storage.lock_timeout", "storage", "Файл занят другим процессом; повторите позже.")
                time.sleep(0.02)
        yield
    finally:
        try:
            if locked:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def decode(raw, validate):
    try:
        data = json.loads(raw.decode("utf-8-sig"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        fail("storage.read_failed", "storage", "Не удалось прочитать корректный JSON-файл.")
    if isinstance(data, dict) and data.get("storage_format") == "food-processes-file":
        if set(data) != {"storage_format", "storage_version", "generation", "document"} or type(data["storage_version"]) is not int or data["storage_version"] != 1 or not isinstance(data["generation"], str) or not re.fullmatch(r"g-[0-9a-f]{32}", data["generation"]):
            fail("storage.unsupported", "compatibility", "Формат сохранённого файла повреждён или не поддерживается.")
        return validate(data["document"]), data["generation"]
    # Older files have no storage token; hash their exact on-disk bytes, not the migrated document.
    return validate(data), "legacy-" + hashlib.sha256(raw).hexdigest()


def read_file(root, path, validate):
    with file_lock(root, path.name):
        if path.is_symlink():
            fail("storage.invalid_target", "storage", "Символическая ссылка вместо файла не поддерживается.")
        document, generation = decode(path.read_bytes(), validate)
        return {"document": document, "generation": generation}


def write_file(root, path, doc, expected, validate):
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"(?:g-[0-9a-f]{32}|legacy-[0-9a-f]{64})", expected)):
        fail("storage.invalid_generation", "input", "Укажите токен поколения файла или null для нового файла.", field="expected_generation")
    temp = None
    with file_lock(root, path.name):
        if path.is_symlink():
            fail("storage.invalid_target", "storage", "Символическая ссылка вместо файла не поддерживается.")
        if path.exists():
            old, actual = decode(path.read_bytes(), validate)
            if expected != actual:
                fail("storage.conflict", "conflict", "Файл изменён другим экземпляром; откройте его и решите, как сохранить данные.", actual_generation=actual)
            if old["session_id"] != doc["session_id"]:
                fail("storage.session_mismatch", "conflict", "Файл содержит другой сеанс; выберите новое имя.")
        elif expected is not None:
            fail("storage.conflict", "conflict", "Файл отсутствует или был удалён; выберите дальнейшее действие.", actual_generation=None)
        generation = "g-" + uuid.uuid4().hex
        payload = {"storage_format": "food-processes-file", "storage_version": 1, "generation": generation, "document": doc}
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=root, prefix=".session-", suffix=".tmp", delete=False) as f:
                temp = Path(f.name)
                json.dump(payload, f, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
            temp = None
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
        return {"target": path.name, "saved_revision": doc["revision"], "generation": generation}
