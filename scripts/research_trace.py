#!/usr/bin/env python3
"""Read-only research trace: how the research programme worked at every step.

Commands: funnel, routes, ai, product <id or name>, summary.
Shared options: --since, --as-of, --json, --out, --project-root, --data-root.

Reads saved records only. Takes no lock, writes nothing into the research
data, contacts no website or provider and calls no model.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.research_trace import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(project_root=ROOT))
