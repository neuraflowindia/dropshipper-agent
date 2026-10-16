"""The read-only research trace: commands ``funnel``, ``routes``, ``ai``,
``product`` and ``summary`` (entry point ``scripts/research_trace.py``).

It reads the saved research records, run logs, AI job folders and spend-watch
readings; it never takes the programme's lock, writes into the research
data, contacts a website or provider, or calls a model. Output goes to the
screen or to ``--out`` (refused inside the research data folder).
"""

import argparse
import json
from pathlib import Path
import sys
import time

from .models import SafetyError, timestamp, utc_now
from .trace_ai import NOT_RECORDED, ai_report
from .trace_funnel import funnel, judge
from .trace_product import find_types, journey, type_name
from .trace_records import TraceError, default_since, load_store, resolve_data_root
from .trace_routes import routes_report
from . import trace_text as text

COMMANDS = ("funnel", "routes", "ai", "product", "summary")
NEEDS_JUDGEMENT = ("funnel", "product", "summary")
NOT_TRACEABLE = (
    NOT_RECORDED + " To fix: save each allowance reading with its time (a small history), or the allowance "
    "percent before and after each AI job.",
    "Raw results the programme read but did not keep (cards it could not recognise, listings outside the search "
    "scope, duplicates) are not saved, so 'seen' counts only what was kept. To fix: save a per-read count of cards "
    "seen / kept / dropped with the drop reason.",
    "Which listing created a product type is not saved as such; the trace takes the match record saved in the "
    "same event as the new type (this credits every type on the live store; a type without one is shown as "
    "'not recorded' or credited to its heading-search wave). To fix: save the creating listing id on each new type.",
    "When a product type first became report-ready (or stopped being) is not saved; the report pool is worked "
    "out again each round. To fix: save each type's report-ready reasons when they change.",
    "The opportunity score and label history per type are not saved (only the one-time calibration). To fix: "
    "save each type's score parts when they change.",
    "Why a round chose some questions and not others (the queue order) is not logged; only 'round limit used up' "
    "is. To fix: log the chosen and skipped question ids per round with the rule that ranked them.",
    "Website time for discovery, supplier and photo reads outside the product-check step is not saved as "
    "'website time'; only 'about to send' and 'answer saved' times are, which include waiting inside the "
    "programme. To fix: save the transport seconds on each read's answer record.",
    "A saved AI stop ('problem') is written once and never closed. The trace checks whether the step's result "
    "exists now, but not when the wait ended or how many times the item waited. To fix: close the problem "
    "record with the time the step finally ran.",
    "The AI runner reports input, cached-input and output tokens only (no reasoning tokens), and the time an AI "
    "step waited before its request was saved (round limits, allowance) is not kept per job. To fix: save the "
    "runner's full usage and the time the step first became ready.",
    "Run logs before research restarted (and logs removed by housekeeping) are outside this store; the trace "
    "covers only this store's records. To fix: point --data-root at an archived store to trace it.",
)


def _report(command, store, *, since, as_of, query=None, judged=None):
    if command == "funnel":
        return funnel(store, judged, since=since, as_of=as_of)
    if command == "routes":
        return routes_report(store, since)
    if command == "ai":
        return ai_report(store, since)
    if command == "product":
        return journey(store, judged, query)
    return {"kind": "research-trace-summary-v1", "since": since, "as_of": as_of,
            "funnel": funnel(store, judged, since=since, as_of=as_of), "routes": routes_report(store, since),
            "ai": ai_report(store, since), "not_traceable": list(NOT_TRACEABLE),
            "events": store.facts["events"], "run_log_lines": len(store.run_rows)}


def render(command, report):
    if report.get("kind") == "research-trace-product-matches-v1":
        if not report["matches"]:
            return f"No product type matches '{report['query']}'."
        return "\n".join([f"{len(report['matches'])} product types match '{report['query']}'. Use one id:"]
                         + [f"   {row['concept_id']}  {row['name']}" for row in report["matches"]])
    return {"funnel": text.funnel_text, "routes": text.routes_text, "ai": text.ai_text,
            "product": text.product_text, "summary": text.summary_text}[command](report)


def _shared(default):
    """The shared options, accepted before or after the command. After the
    command their default is "not given", so it never hides a value given before."""
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--since", default=default,
                        help="Count work saved at or after this time (ISO, e.g. 2026-10-16T06:58:54+00:00). "
                             "Default: the owner meter's saved start.")
    shared.add_argument("--as-of", default=default, help="Judge product types as of this time (default: now).")
    shared.add_argument("--json", action="store_true", default=default, help="Print JSON instead of plain text.")
    shared.add_argument("--out", default=default, help="Write the output to this file instead of the screen.")
    shared.add_argument("--project-root", default=default, help="Project folder (default: this script's project).")
    shared.add_argument("--data-root", default=default,
                        help="Folder holding data/ (default: the project's runtime storage setting).")
    return shared


def parser():
    top = argparse.ArgumentParser(description="Read-only research trace: how the research worked at every step.",
                                  parents=[_shared(None)])
    top.set_defaults(json=False)
    commands = top.add_subparsers(dest="command", required=True)
    for name, help_text in (("funnel", "Every step in order: how many passed and why others stopped."),
                            ("routes", "Every website: reads, answers, reuse, failures, waits, time per read."),
                            ("ai", "AI use per step: jobs, outcomes, tokens and time."),
                            ("product", "One product type's journey (give its id or part of its name)."),
                            ("summary", "All of the above, short.")):
        sub = commands.add_parser(name, help=help_text, parents=[_shared(argparse.SUPPRESS)])
        if name == "product":
            sub.add_argument("query", help="Product type id, or part of its name.")
    return top


def _out_path(value, data_root):
    path = Path(value).absolute()
    if path == data_root or data_root in path.parents:
        raise TraceError("TRACE_OUTPUT_INSIDE_RESEARCH_DATA")
    return path


def main(argv=None, *, project_root=None, now=None, stdout=None):
    args = parser().parse_args(argv)
    stdout = stdout or sys.stdout
    started = time.monotonic()
    try:
        root = Path(args.project_root or project_root or Path(__file__).resolve().parents[2])
        data_root = resolve_data_root(root, args.data_root)
        out = _out_path(args.out, data_root) if args.out else None
        as_of = args.as_of or now or utc_now()
        timestamp(as_of)
        if args.since:
            timestamp(args.since)
        store = load_store(data_root)
        since = args.since or default_since(store)
        query = getattr(args, "query", None)
        matches = find_types(store, query) if args.command == "product" else None
        if matches is not None and len(matches) != 1:
            report = {"kind": "research-trace-product-matches-v1", "query": query,
                      "matches": [{"concept_id": cid, "name": type_name(store.state, cid)} for cid in matches]}
        else:
            judged = judge(store, as_of) if args.command in NEEDS_JUDGEMENT else None
            report = _report(args.command, store, since=since, as_of=as_of, query=matches[0] if matches else None,
                             judged=judged)
        if args.command == "summary":
            report["seconds"] = round(time.monotonic() - started, 1)
        body = json.dumps(report, indent=1, ensure_ascii=False, default=str) if args.json else render(args.command, report)
    except SafetyError as error:
        print(f"The trace could not run: {text.words.reason(str(error))}", file=sys.stderr)
        return 1
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body + "\n", encoding="utf-8")
    else:
        print(body, file=stdout)
    if report.get("kind") == "research-trace-product-matches-v1":
        return 2
    return 0
