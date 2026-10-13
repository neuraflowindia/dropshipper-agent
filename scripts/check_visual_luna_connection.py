#!/usr/bin/env python3
"""Opt-in two-public-photo Luna check; never a product association decision."""

import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import SafetyError, timestamp, utc_now
from product_radar.research_ai import CodexRunner, Jobs, MODEL, current_attempt, prompt_version
from product_radar.research_memory import ResearchMemory, key
from product_radar.runtime_storage import logical_path, runtime_path
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.visual_identity import prepare_visual_comparison

PHOTOS = runtime_path(ROOT, "media/live/product-images")
RECEIPTS = tuple(PHOTOS / f"visual-probe-20261006/image-{n}-receipt.json" for n in (1, 2))
OBSERVATIONS = ("obs-760eeb69e9c97dcde8c214e7b5a421dbc3da6546", "obs-1d228e2fa2b5a89e46ebf79d2002629b6624e2dd")
OUTPUT = runtime_path(ROOT, "data/benchmarks/2026-10-06/visual_luna_probe.json")
CONFIG = ROOT / "config/ai/codex_luna.json"


def contains_exact(value, wanted):
    """Find the original complete URL, not a title/substring inference."""
    if isinstance(value, dict):
        return any(contains_exact(child, wanted) for child in value.values())
    if isinstance(value, list):
        return any(contains_exact(child, wanted) for child in value)
    return isinstance(value, str) and value == wanted


def prepare_probe(state, receipts, *, media_root):
    """Bind the fixed pair to immutable observations and saved A descriptions."""
    if not isinstance(receipts, list) or len(receipts) != 2:
        raise SafetyError("VISUAL_PROBE_RECEIPTS_INVALID")
    descriptions, semantics, photos = [], [], []
    owners = ("visual-probe-chair-cover", "visual-probe-mattress-protector")
    for n, (receipt, expected_observation, owner) in enumerate(zip(receipts, OBSERVATIONS, owners), 1):
        try:
            if (receipt["observation_id"] != expected_observation or receipt["source_kind"] != "public_product_image"
                    or receipt["state"] != "Captured" or receipt["status"] != 200
                    or receipt["path"] != f"visual-probe-20261006/image-{n}.jpg"):
                raise SafetyError("VISUAL_PROBE_RECEIPTS_INVALID")
            observation = state["observations"].get(expected_observation)
            if observation is None:
                raise SafetyError("VISUAL_PROBE_OBSERVATION_MISSING")
            if (receipt["source_observed_at"] != observation["observed_at"]
                    or not contains_exact({"semantic": observation["semantic"], "facts": observation["facts"]}, receipt["source_url"])):
                raise SafetyError("VISUAL_PROBE_SOURCE_BINDING_INVALID")
            timestamp(receipt["collected_at"])
            if timestamp(receipt["collected_at"]) < timestamp(receipt["source_observed_at"]):
                raise SafetyError("VISUAL_PROBE_SOURCE_BINDING_INVALID")
            semantic_id = observation["semantic_id"]
            saved = state["descriptions"].get(semantic_id)
            if not saved or not saved["result"]["products"]:
                raise SafetyError("VISUAL_PROBE_DESCRIPTION_MISSING")
            if saved["result"]["id"] != semantic_id:
                raise SafetyError("VISUAL_PROBE_SOURCE_BINDING_INVALID")
            descriptions.append(saved["result"]["products"][0])
            semantics.append(semantic_id)
            photos.append({"id": f"visual-probe-image-{n}", "owner_id": owner, "path": receipt["path"],
                           "sha256": receipt["sha256"], "source_kind": receipt["source_kind"],
                           "source_ref": expected_observation, "source_url": receipt["source_url"],
                           "observed_at": receipt["source_observed_at"]})
        except (KeyError, TypeError, IndexError):
            raise SafetyError("VISUAL_PROBE_RECEIPTS_INVALID") from None
    prepared = prepare_visual_comparison(owners[0], semantics[0], descriptions[0],
                                        [{"id": owners[1], "description": descriptions[1]}], photos, media_root=media_root)
    for photo, receipt in zip(prepared["request"]["payload"]["images"], receipts):
        if type(receipt.get("bytes")) is not int or photo["size"] != receipt["bytes"]:
            raise SafetyError("VISUAL_PROBE_SOURCE_BINDING_INVALID")
    return prepared


def safe_code(error):
    code = str(error) if isinstance(error, SafetyError) else "VISUAL_PROBE_LOCAL_FAILURE"
    return code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", code) else "VISUAL_PROBE_LOCAL_FAILURE"


def run_probe():
    """Called only after --live; use normal durable jobs, never auto-retry."""
    report = {"schema_version": 1, "probe_only": True, "scope": "chair_cover_vs_mattress_protector",
              "model": MODEL, "authentication": "chatgpt", "applied_associations": 0,
              "checked_at": utc_now(), "status": "failed", "job_id": None,
              "result": None, "usage": None, "image_input_contract": "unproved"}
    try:
        memory = ResearchMemory(runtime_path(ROOT, "data/research/captured"), Journal(runtime_path(ROOT, "data/benchmarks/state/apify")))
        prepared = prepare_probe(memory.snapshot(), [load_json(path) for path in RECEIPTS], media_root=PHOTOS)
        payload = prepared["request"]["payload"]
        runner = CodexRunner(load_json(CONFIG))
        report["job_id"] = key("job", {"role": "D", "payload": payload, "model": MODEL,
                                      "prompt_version": prompt_version("D"), "runner": runner.identity})
        job_id, result = Jobs(memory, runner).run("D", payload)
        report["job_id"], report["result"] = job_id, result
        saved = load_json(current_attempt(memory.root / "ai_jobs" / job_id) / "result.json")
        report["usage"] = saved["usage"]
        verdict = result["accepted"].get(payload["items"][0]["id"])
        if verdict is not None:
            report["image_input_contract"] = "accepted"
            report["expected_decision"] = "different"
            report["expected_comparison_match"] = verdict["decision"] == "different"
            report["status"] = "passed" if report["expected_comparison_match"] else "needs_review"
        else:
            report["error_code"] = "VISUAL_PROBE_RESULT_NOT_ACCEPTED"
    except Exception as error:
        report["error_code"] = safe_code(error)
    # Preserve each actual prompt/job outcome; the default file is a current view.
    if report["job_id"] is not None:
        atomic_json(OUTPUT.parent / "visual_luna_probes" / (report["job_id"] + ".json"), report)
    atomic_json(OUTPUT, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use subscription allowance for this one saved public-photo pair.")
    args = parser.parse_args(argv)
    if not args.live:
        print("No model request or report write made. Use --live for the two saved public-photo check.")
        return 2
    try:
        report = run_probe()
    except Exception:
        print("CHECK FAILED: VISUAL_PROBE_LOCAL_FAILURE. No private diagnostics were printed.")
        return 1
    print(f"Visual check: {report['status']}. Saved report: {logical_path(ROOT, OUTPUT) or OUTPUT}")
    if report.get("error_code"):
        print("Reason: " + report["error_code"])
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
