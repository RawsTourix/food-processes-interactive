"""Controlled subprocess used by storage concurrency tests."""
import json
from pathlib import Path
import sys
from food_processes import Application, storage


def request(app, op, **args):
    result = app.request({"api_version": "0.1", "request_id": "worker", "op": op, "args": args})
    if result["outcome"] != "ok":
        raise RuntimeError(result)
    return result["data"]


root, mode = Path(sys.argv[1]), sys.argv[2]
if mode == "hold":
    with storage.file_lock(root, "shared.json"):
        print(json.dumps({"ready": True}), flush=True)
        sys.stdin.readline()
elif mode == "write":
    point = sys.argv[3]
    app = Application(root)
    opened = request(app, "storage.read", target="shared.json")
    doc, token = opened["document"], opened["generation"]
    sid = request(app, "session.restore", document=doc)["session_id"]
    revision = doc["revision"]
    request(app, "session.command", session_id=sid, expected_revision=revision, action="select_region", params={"region_id": "schematic-area-a", "point_id": point})
    edited = request(app, "session.save", session_id=sid, expected_revision=revision + 1)["document"]
    print(json.dumps({"ready": True, "generation": token}), flush=True)
    if sys.stdin.readline().strip() != "go":
        raise RuntimeError("synchronization cancelled")
    result = app.request({"api_version": "0.1", "request_id": "worker", "op": "storage.write", "args": {"document": edited, "target": "shared.json", "expected_generation": token}})
    print(json.dumps(result, ensure_ascii=False), flush=True)
elif mode in ("writer-loop", "reader-loop"):
    app = Application(root)
    opened = request(app, "storage.read", target="shared.json")
    sid = request(app, "session.restore", document=opened["document"])["session_id"] if mode == "writer-loop" else None
    token = opened["generation"]
    rev = opened["document"]["revision"]
    print(json.dumps({"ready": True}), flush=True)
    for i in range(1, 11):
        if sys.stdin.readline().strip() != "step":
            raise RuntimeError("synchronization cancelled")
        if mode == "writer-loop":
            request(app, "session.command", session_id=sid, expected_revision=rev, action="select_region", params={"region_id": "schematic-area-a", "point_id": "v" + str(i)})
            rev += 1
            doc = request(app, "session.save", session_id=sid, expected_revision=rev)["document"]
            token = request(app, "storage.write", target="shared.json", document=doc, expected_generation=token)["generation"]
            result = {"revision": rev}
        else:
            doc = request(app, "storage.read", target="shared.json")["document"]
            result = {"revision": doc["revision"], "point": doc["module_data"]["selection"]["point_id"]}
        print(json.dumps(result), flush=True)
