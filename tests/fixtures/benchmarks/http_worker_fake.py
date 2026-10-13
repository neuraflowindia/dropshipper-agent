"""Offline subprocess fixture; never opens a socket or reads an owner key."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time

mode, output = sys.argv[1], Path(sys.argv[-1])
raw = sys.stdin.buffer.read(65537)
spec = json.loads(raw)
if mode == "delay":
    time.sleep(60)
elif mode == "malformed":
    (output / "result.json").write_bytes(b"{}"); raise SystemExit(0)
elif mode == "inspect":
    # Parent stdout/stderr must be discarded, including a reflected secret.
    print(spec["token"])
    print(spec["token"], file=sys.stderr)
body = json.dumps({"argv": sys.argv, "environment": sorted(os.environ)}).encode() if mode == "inspect" else b'{"data":{}}'
(output / "body.bin").write_bytes(body)
(output / "result.json").write_text(json.dumps({"ok": True, "status": 200, "headers": {"retry-after": spec["token"]} if mode == "header_reflection" else {}, "elapsed": 0,
                                             "bytes": len(body), "hash": hashlib.sha256(body).hexdigest()}))
