import copy
import json
import os
import subprocess
import sys

from food_processes import Application


def compare_requests(tmp_path, requests):
    app = Application(tmp_path)
    proc = subprocess.Popen([sys.executable, "-m", "food_processes", "--storage-dir", str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    app_ids, cli_ids, normalized = {}, {}, []
    def normalize(value, mapping):
        if isinstance(value, dict):
            return {k: normalize(v, mapping) for k, v in value.items()}
        if isinstance(value, list):
            return [normalize(v, mapping) for v in value]
        return mapping.get(value, value) if isinstance(value, str) else value
    try:
        for req in requests:
            python_req, cli_req = copy.deepcopy(req), copy.deepcopy(req)
            alias = req["args"].get("session_id")
            if alias in app_ids:
                python_req["args"]["session_id"] = app_ids[alias]
                cli_req["args"]["session_id"] = cli_ids[alias]
            expected = app.request(python_req)
            proc.stdin.write(json.dumps(cli_req, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            assert line, "CLI завершился до ответа"
            actual = json.loads(line)
            if req["op"] == "session.create":
                alias = f"s{len(app_ids) + 1}"
                app_ids[alias] = expected["data"]["session_id"]
                cli_ids[alias] = actual["data"]["session_id"]
            norm_app = normalize(expected, {v: k for k, v in app_ids.items()})
            norm_cli = normalize(actual, {v: k for k, v in cli_ids.items()})
            assert norm_cli == norm_app
            normalized.append(norm_cli)
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
        proc.stdout.close()
        stderr = proc.stderr.read()
        proc.stderr.close()
        assert proc.returncode == 0, stderr
    return normalized
