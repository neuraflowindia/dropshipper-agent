#!/usr/bin/env python3
"""Internal bounded HTTP worker; never invoked directly by the owner."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.http_io import worker_main

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(2)
    raise SystemExit(worker_main(sys.argv[1]))
