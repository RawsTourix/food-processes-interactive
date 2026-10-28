"""Строка JSON запроса → строка JSON ответа; состояние живёт до завершения процесса."""
import argparse
import json
import sys
from .core import Application


def main():
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Headless API: JSON Lines из stdin в stdout")
    parser.add_argument("--storage-dir", help="Доверенный каталог для файлов сеансов")
    args = parser.parse_args()
    app = Application(args.storage_dir)
    for line in sys.stdin:
        try:
            request = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError):
            response = {"api_version": "0.1", "request_id": None, "outcome": "error", "data": None, "diagnostics": [{"code": "json.invalid", "category": "input", "severity": "error", "message": "Некорректная строка JSON."}]}
        else:
            response = app.request(request)
        print(json.dumps(response, ensure_ascii=False, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
