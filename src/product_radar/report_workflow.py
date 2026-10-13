"""Assemble one explicit frozen local draft; never approve, upload or publish."""

from copy import deepcopy
from pathlib import Path
import re

from .delivery_package import validate_delivery_manifest, write_delivery_manifest
from .live_outputs import (build_issue_dataset, digest_bytes, validate_dataset, write_issue_package,
                           write_issue_workbook, write_issue_edition)
from .models import SafetyError, digest, identifier
from .report_narration import LEGACY_MIGRATION_WAIT, generate_report_narratives
from .runtime_storage import runtime_path
from .storage import Journal, atomic_bytes, atomic_json, load_json, safe_path


def _stage_failure(error):
    code = str(error) if isinstance(error, SafetyError) else "REPORT_LOCAL_STAGE_FAILED"
    return code if re.fullmatch(r"[A-Z][A-Z0-9_]{1,199}", code) else "REPORT_LOCAL_STAGE_FAILED"


def _pdf_bytes(path):
    """Read a bounded actual local PDF, never a link or a plausible receipt."""
    safe_path(path.parent)
    if path.is_symlink():
        raise SafetyError("REPORT_SAVED_PDF_INVALID")
    try:
        with path.open("rb") as stream:
            raw = stream.read(32 * 1024 * 1024 + 1)
    except OSError:
        raise SafetyError("REPORT_SAVED_PDF_MISSING") from None
    if not raw or len(raw) > 32 * 1024 * 1024:
        raise SafetyError("REPORT_SAVED_PDF_INVALID")
    return raw


def _measured_pdf(path, html_hash, inspect_pdf):
    raw = _pdf_bytes(path)
    return raw, dict(inspect_pdf(raw), path=str(path), html_sha256=html_hash,
                     pdf_sha256=digest_bytes(raw))


def _save_pdf_receipt(path, receipt):
    try:
        atomic_json(path, receipt)
    except (SafetyError, OSError):
        # The atomic write may have succeeded before its reply was lost.
        # Read that exact receipt instead of re-rendering the owned PDF.
        if not path.exists() or load_json(path) != receipt:
            raise


def _render_edition(data, input_hash, language, edition, render_pdf, inspect_pdf):
    """Recover one owned edition without depending on the other language."""
    html_record = edition["artifacts"]["html"]
    html = Path(html_record["path"])
    pdf = html.with_suffix(".pdf")
    staged = safe_path(data, "internal-stages")
    receipt_path = staged / ("pdf-receipt-" + language + ".json")
    intent_path = staged / ("pdf-intent-" + language + ".json")
    intent = {"input_hash": input_hash, "html_sha256": html_record["sha256"], "path": str(pdf)}
    if intent_path.exists():
        if load_json(intent_path) != intent:
            raise SafetyError("REPORT_RENDER_INTENT_CHANGED")
    elif pdf.exists() or receipt_path.exists():
        raise SafetyError("REPORT_RENDER_PROVENANCE_REQUIRED")
    else:
        atomic_json(intent_path, intent)
    if not callable(inspect_pdf):
        raise SafetyError("REPORT_NATIVE_RENDERER_REQUIRED")
    if not pdf.exists():
        # Earlier versions rendered directly to the final alias. Reuse that
        # exact owned result only when its original intent binds the same HTML.
        # A file without an original intent remains untrusted, not a new PDF.
        final_pdf = html.parent.parent / pdf.name
        if final_pdf.exists():
            legacy_intent = data / ("pdf-intent-" + language + ".json")
            expected = dict(intent, path=str(final_pdf))
            if not legacy_intent.exists() or load_json(legacy_intent) != expected:
                raise SafetyError("REPORT_RENDER_PROVENANCE_REQUIRED")
            _, measured = _measured_pdf(final_pdf, html_record["sha256"], inspect_pdf)
            legacy_receipt = data / ("pdf-receipt-" + language + ".json")
            if legacy_receipt.exists():
                if load_json(legacy_receipt) != measured:
                    raise SafetyError("REPORT_SAVED_PDF_CHANGED")
            else:
                _save_pdf_receipt(legacy_receipt, measured)
            return measured
        if receipt_path.exists():
            raise SafetyError("REPORT_SAVED_PDF_MISSING")
        if not callable(render_pdf):
            raise SafetyError("REPORT_NATIVE_RENDERER_REQUIRED")
        returned = render_pdf(html, pdf)
        _, measured = _measured_pdf(pdf, html_record["sha256"], inspect_pdf)
        if returned != measured:
            raise SafetyError("REPORT_RENDER_RECEIPT_INVALID")
        _save_pdf_receipt(receipt_path, measured)
    else:
        _, measured = _measured_pdf(pdf, html_record["sha256"], inspect_pdf)
        if not receipt_path.exists():
            _save_pdf_receipt(receipt_path, measured)
        if load_json(receipt_path) != measured:
            raise SafetyError("REPORT_SAVED_PDF_CHANGED")
    return measured


def _promote_pdf(data, input_hash, language, draft, rendered, inspect_pdf):
    """Copy checked internal bytes into the final alias; never render again."""
    html = Path(draft["artifacts"]["html"]["path"])
    pdf = html.with_suffix(".pdf")
    intent_path = data / ("pdf-intent-" + language + ".json")
    receipt_path = data / ("pdf-receipt-" + language + ".json")
    intent = {"input_hash": input_hash, "html_sha256": draft["artifacts"]["html"]["sha256"], "path": str(pdf)}
    if rendered["html_sha256"] != intent["html_sha256"]:
        raise SafetyError("REPORT_RENDER_INTENT_CHANGED")
    if intent_path.exists():
        if load_json(intent_path) != intent:
            raise SafetyError("REPORT_RENDER_INTENT_CHANGED")
    elif pdf.exists() or receipt_path.exists():
        raise SafetyError("REPORT_RENDER_PROVENANCE_REQUIRED")
    else:
        atomic_json(intent_path, intent)
    raw, original = _measured_pdf(Path(rendered["path"]), rendered["html_sha256"], inspect_pdf)
    if original != rendered:
        raise SafetyError("REPORT_SAVED_PDF_CHANGED")
    expected = dict(rendered, path=str(pdf))
    if pdf.exists():
        _, actual = _measured_pdf(pdf, intent["html_sha256"], inspect_pdf)
        if actual != expected:
            raise SafetyError("REPORT_SAVED_PDF_CHANGED")
    else:
        try:
            atomic_bytes(pdf, raw)
        except (SafetyError, OSError):
            if not pdf.exists() or _measured_pdf(pdf, intent["html_sha256"], inspect_pdf)[1] != expected:
                raise
    if receipt_path.exists():
        if load_json(receipt_path) != expected:
            raise SafetyError("REPORT_SAVED_PDF_CHANGED")
    else:
        _save_pdf_receipt(receipt_path, expected)
    return expected


def _presentation_source(root, issue, product_facts, evidence_type, revision, choice):
    """Verify an earlier complete packet before reusing only its accepted prose."""
    if type(revision) is not int or revision < 1 or revision >= issue["revision"]:
        raise SafetyError("REPORT_PRESENTATION_REVISION_INVALID")
    folder = safe_path(runtime_path(root, "data", "outputs"), issue["issue_id"], "revision-" + str(revision))
    frozen = load_json(folder / "package-input.json", 8 * 1024 * 1024)
    if not isinstance(frozen, dict):
        raise SafetyError("REPORT_PRESENTATION_SOURCE_INVALID")
    original_issue = frozen.get("issue")
    if not isinstance(original_issue, dict) or original_issue.get("revision") != revision:
        raise SafetyError("REPORT_PRESENTATION_SOURCE_INVALID")
    for value in (original_issue, issue):
        if not isinstance(value.get("input_hash"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["input_hash"]):
            raise SafetyError("REPORT_PRESENTATION_ALLOCATION_HASH_INVALID")
    # The allocator hashes its revision-bearing inputs. This provenance hash
    # naturally changes with revision; all actual report/policy fields remain
    # exact, and each full issue keeps its own hash in its frozen input.
    comparable = lambda value: {key: item for key, item in value.items()
                                if key not in ("revision", "selection_hash", "input_hash")}
    if (digest(comparable(original_issue)) != digest(comparable(issue))
            or digest(frozen.get("product_facts")) != digest(product_facts)
            or frozen.get("evidence_type") != evidence_type or frozen.get("schema_version") != 1):
        raise SafetyError("REPORT_PRESENTATION_INPUT_CHANGED")
    manifest = load_json(folder / "delivery-manifest.json", 512 * 1024)
    status = load_json(folder / "package-status.json", 512 * 1024)
    if not isinstance(manifest, dict) or not isinstance(status, dict):
        raise SafetyError("REPORT_PRESENTATION_SOURCE_INVALID")
    if (manifest.get("complete") is not True or manifest.get("approved") is not False
            or manifest.get("release_ready") is not False
            or manifest.get("state") != "draft_ready_for_review"
            or status.get("complete") is not True or status.get("approved") is not False
            or status.get("client_ready") is not False or status.get("state") != "draft_ready_for_review"
            or status.get("issue_id") != issue["issue_id"] or status.get("revision") != revision
            or status.get("input_hash") != digest(frozen)
            or status.get("package_hash") != manifest.get("package_hash")):
        raise SafetyError("REPORT_PRESENTATION_COMPLETE_SOURCE_REQUIRED")
    if manifest.get("selected_language") != choice or manifest.get("evidence_type") != evidence_type:
        raise SafetyError("REPORT_PRESENTATION_INPUT_CHANGED")
    # This validator reads and hashes the source HTML, PDFs, workbook and actual
    # fact file. A saved status or plausible narrative alone cannot authorise reuse.
    validate_delivery_manifest(manifest, original_issue, root=root, selected_language=choice)
    saved_dataset = load_json(folder / "final_report_data.json", 4 * 1024 * 1024)
    needed = {"en", "hi-Latn"} if issue["projected_ordinal"] <= 2 else {choice}
    if not isinstance(saved_dataset, dict) or not needed.issubset(saved_dataset.get("narratives", {})):
        raise SafetyError("REPORT_PRESENTATION_LANGUAGE_REQUIRED")
    reconstructed = build_issue_dataset(original_issue, product_facts,
                                        narratives=saved_dataset.get("narratives"), evidence_type=evidence_type)
    reconstructed["selected_language"] = choice
    if digest(reconstructed) != digest(saved_dataset):
        raise SafetyError("REPORT_PRESENTATION_FACTS_CHANGED")
    origin = {"revision": revision, "package_hash": manifest["package_hash"],
              "facts_hash": manifest["facts_hash"], "product_facts_hash": digest(product_facts),
              "input_hash": digest(frozen), "allocation_input_hash": original_issue["input_hash"]}
    # These are original source job IDs, not newly executed writing jobs.
    jobs = deepcopy(status.get("source_job_ids", status.get("job_ids", [])))
    return deepcopy(saved_dataset["narratives"]), origin, jobs


def freeze_review_input(memory, issue, product_facts, *, root):
    """Save exact verified input before a new type packet takes product holds.

    This has no writing, collection or publication effect. A crash before the
    hold can resume the existing immutable input through ordinary adoption.
    """
    from .report_examples import RECOMMENDATION_CONTRACT
    if (issue.get("recommendation_contract") != RECOMMENDATION_CONTRACT
            or issue.get("ready_for_review") is not True):
        raise SafetyError("REPORT_FUNCTIONAL_INPUT_REQUIRED")
    dataset = build_issue_dataset(issue, product_facts, evidence_type=memory.evidence_type)
    validate_dataset(issue, dataset, root=root)
    data = safe_path(runtime_path(root, "data", "outputs"), issue["issue_id"], "revision-" + str(issue["revision"]))
    binding = {"schema_version": 1, "issue": deepcopy(issue), "product_facts": deepcopy(product_facts),
               "evidence_type": memory.evidence_type}
    with Journal(data / "package_writer").locked():
        path = data / "package-input.json"
        if path.exists():
            if load_json(path, 8 * 1024 * 1024) != binding:
                raise SafetyError("REPORT_FROZEN_INPUT_CHANGED")
        else:
            atomic_json(path, binding)
    return binding


def prepare_review_package(memory, issue, product_facts, *, root, runner=None,
                           render_pdf=None, inspect_pdf=None, selected_language=None,
                           presentation_source_revision=None, allow_fresh_legacy_model=True):
    """Connect checked issue → saved G jobs → shared files → checked PDF packet.

    Browser functions are injected from the existing native renderer. No model
    or browser runs for a short issue or an unfinished later language choice.
    Replaying exact inputs verifies/reuses files; changed inputs need a revision.
    A complete return is still an internal draft needing human/visual review.
    Explicit presentation reuse verifies an earlier complete packet and never
    invokes a writing model, even if a runner is supplied.
    """
    if not isinstance(issue, dict):
        raise SafetyError("REPORT_ISSUE_REQUIRED")
    if type(allow_fresh_legacy_model) is not bool:
        raise SafetyError("REPORT_NARRATION_OPTIONS_INVALID")
    identifier(issue.get("issue_id"))
    if selected_language not in (None, "en", "hi-Latn"):
        raise SafetyError("REPORT_LANGUAGE_INVALID")
    if issue.get("ready_for_review") is not True:
        return {"issue_id": issue["issue_id"], "state": "shortfall", "shortfall": issue.get("shortfall"),
                "approved": False, "client_ready": False, "collector_requests": 0}
    dataset = build_issue_dataset(issue, product_facts, evidence_type=memory.evidence_type)
    ordinal = issue.get("projected_ordinal")
    if type(ordinal) is not int or ordinal < 1:
        raise SafetyError("REPORT_ORDINAL_INVALID")
    root = safe_path(root)
    choice = None if ordinal <= 2 else selected_language
    origin, source_jobs = None, []
    if presentation_source_revision is not None:
        narratives, origin, source_jobs = _presentation_source(
            root, issue, product_facts, memory.evidence_type, presentation_source_revision, choice)
        dataset["narratives"] = narratives
        dataset["selected_language"] = choice
        validate_dataset(issue, dataset)
    data = safe_path(runtime_path(root, "data", "outputs"), issue["issue_id"], "revision-" + str(issue["revision"]))
    binding = {"schema_version": 1, "issue": deepcopy(issue), "product_facts": deepcopy(product_facts),
               "evidence_type": memory.evidence_type}
    if origin is not None:
        binding["presentation_source"] = origin
    with Journal(data / "package_writer").locked():
        source = data / "package-input.json"
        if source.exists():
            if load_json(source, 8 * 1024 * 1024) != binding:
                raise SafetyError("REPORT_FROZEN_INPUT_CHANGED")
        else:
            atomic_json(source, binding)
        status = {"issue_id": issue["issue_id"], "revision": issue["revision"], "input_hash": digest(binding),
                  "approved": False, "client_ready": False, "collector_requests": 0}
        if origin is not None:
            status.update(presentation_source=origin, source_job_ids=source_jobs, job_ids=[])
        final = data / "delivery-manifest.json"
        if final.exists():
            manifest = load_json(final, 512 * 1024)
            validate_delivery_manifest(manifest, issue, root=root, selected_language=choice)
            status.update(state="draft_ready_for_review", complete=manifest["complete"],
                          manifest_path=str(final), package_hash=manifest["package_hash"], reused=True)
            # Readiness comes from the actual checked packet above. An exact
            # completed status may retain attribution/diagnostics, but cannot
            # supply readiness, paths, approval or another packet's metadata.
            previous = None
            try:
                previous = load_json(data / "package-status.json", 512 * 1024)
            except SafetyError as error:
                if str(error) not in ("SAVED_FILE_UNAVAILABLE", "SAVED_FILE_SIZE_LIMIT", "JSON_RESPONSE_INVALID"):
                    raise
            matching = isinstance(previous, dict) and all(
                type(previous.get(field)) is type(value) and digest(previous.get(field)) == digest(value)
                for field, value in status.items() if field not in ("reused", "job_ids", "source_job_ids"))
            matching = matching and ("reused" not in previous or type(previous["reused"]) is bool)
            if matching:
                attribution = {field: previous[field] for field in ("job_ids", "source_job_ids") if field in previous}
                matching = all(isinstance(values, list)
                               and all(isinstance(value, str) and re.fullmatch(r"job-[0-9a-f]{40}", value)
                                       for value in values) and len(set(values)) == len(values)
                               for values in attribution.values())
                if origin is not None:
                    matching = matching and attribution.get("job_ids", []) == [] and attribution.get("source_job_ids") == source_jobs
                else:
                    matching = matching and "source_job_ids" not in attribution and "presentation_source" not in previous
                stages = previous.get("stage_outcomes")
                if "stage_outcomes" in previous:
                    matching = matching and isinstance(stages, dict) and set(stages) == {"workbook", "writing", "editions"}
                    if matching:
                        workbook, writing, editions = (stages[field] for field in ("workbook", "writing", "editions"))
                        matching = (isinstance(workbook, dict) and set(workbook) == {"state", "receipt"}
                                    and workbook.get("state") == "completed"
                                    and isinstance(workbook.get("receipt"), dict)
                                    and isinstance(writing, dict)
                                    and {"state", "complete_languages", "failures"} <= set(writing)
                                    and set(writing) <= {"state", "complete_languages", "failures", "problem_save_failures"}
                                    and writing.get("state") == "completed"
                                    and isinstance(writing.get("complete_languages"), list)
                                    and all(isinstance(language, str) and language in ("en", "hi-Latn")
                                            for language in writing["complete_languages"])
                                    and len(set(writing["complete_languages"])) == len(writing["complete_languages"])
                                    and set(manifest["languages"]) <= set(writing["complete_languages"])
                                    and writing.get("failures") == {} and writing.get("problem_save_failures", {}) == {}
                                    and isinstance(editions, dict) and set(editions) == set(manifest["languages"]))
                        if matching:
                            receipt = workbook["receipt"]
                            matching = (receipt.get("complete") is True and receipt.get("approved") is False
                                        and receipt.get("release_ready") is False
                                        and receipt.get("issue_id") == issue["issue_id"]
                                        and type(receipt.get("revision")) is int and receipt["revision"] == issue["revision"]
                                        and isinstance(receipt.get("artifacts"), dict)
                                        and isinstance(receipt["artifacts"].get("workbook"), dict)
                                        and receipt["artifacts"]["workbook"].get("sha256") == manifest["artifacts"]["workbook"]["sha256"])
                            if matching:
                                # New packets already bind the entire original
                                # workbook receipt in their checked provenance.
                                matching = all(draft.get("internal_stage", {}).get("workbook_receipt_hash") == digest(receipt)
                                               for draft in manifest["language_manifests"].values())
                        if matching:
                            for language, edition in editions.items():
                                pdf = edition.get("pdf") if isinstance(edition, dict) else None
                                html = edition.get("html") if isinstance(edition, dict) else None
                                expected = manifest["pdf_receipts"][language]
                                matching = (isinstance(edition, dict) and set(edition) == {"state", "html", "pdf"}
                                            and edition.get("state") == "completed"
                                            and isinstance(html, dict) and set(html) == {"path", "sha256", "bytes"}
                                            and html.get("sha256") == expected["html_sha256"]
                                            and type(html.get("bytes")) is int
                                            and html["bytes"] == manifest["artifacts"]["html-" + language]["size"]
                                            and html.get("path") in (manifest["artifacts"]["html-" + language]["path"],
                                                str(Path(expected["path"]).parent / "internal-stages" / ("report-" + language + ".html")))
                                            and isinstance(pdf, dict) and set(pdf) == set(expected) and all(
                                                type(pdf.get(field)) is type(value) and pdf.get(field) == value
                                                for field, value in expected.items() if field != "path")
                                            and pdf.get("path") in (expected["path"],
                                                str(Path(expected["path"]).parent / "internal-stages" / Path(expected["path"]).name)))
                                if not matching:
                                    break
                if matching:
                    status.update(deepcopy(attribution))
                    if stages is not None:
                        status["stage_outcomes"] = deepcopy(stages)
            atomic_json(data / "package-status.json", status)
            return status
        # Facts and the workbook do not depend on a customer's future language
        # choice or on a writing/browser failure. Preserve their own checked
        # stage, not a falsely final dataset with unfinished writing.
        stages = {}
        try:
            workbook = write_issue_workbook(issue, dataset, root)
            stages["workbook"] = {"state": "completed", "receipt": workbook}
        except Exception as error:
            stages["workbook"] = {"state": "waiting", "reason": _stage_failure(error),
                                  "error_type": type(error).__name__}
        status["stage_outcomes"] = stages
        if ordinal > 2 and selected_language is None:
            status.update(state="language_pending", complete=False)
            atomic_json(data / "package-status.json", status)
            return status
        languages = ("en", "hi-Latn") if ordinal <= 2 else (selected_language,)
        dataset["selected_language"] = choice
        if origin is None:
            try:
                result = generate_report_narratives(memory, issue, dataset, runner, languages=languages,
                    allow_fresh_legacy_model=allow_fresh_legacy_model)
            except Exception as error:
                result = {"dataset": dataset, "job_ids": [], "failures": {
                    language: {"edition": _stage_failure(error)} for language in languages},
                    "complete_languages": []}
        else:
            result = {"dataset": dataset, "job_ids": [], "failures": {},
                      "complete_languages": list(dataset["narratives"])}
        writing_complete = not result["failures"] and not set(languages) - set(result["complete_languages"])
        stages["writing"] = {"state": "completed" if writing_complete else "partial",
                             "complete_languages": result["complete_languages"], "failures": result["failures"],
                             "problem_save_failures": result.get("problem_save_failures", {})}
        drafts, receipts, editions = {}, {}, {}
        for language in languages:
            if language not in result["complete_languages"]:
                editions[language] = {"state": "waiting", "reason": "REPORT_WRITING_NOT_COMPLETE"}
                continue
            try:
                edition = write_issue_edition(issue, result["dataset"], root, language=language)
                editions[language] = {"state": "html_completed", "html": edition["artifacts"]["html"]}
                rendered = _render_edition(data, digest(binding), language, edition, render_pdf, inspect_pdf)
                editions[language].update(state="completed", pdf=rendered)
                if writing_complete and stages["workbook"]["state"] == "completed":
                    # Final facts are frozen only once required writing is
                    # complete. A healthy edition may finish while the other
                    # native render waits, without rerendering either result.
                    draft = write_issue_package(issue, result["dataset"], root, language=language)
                    receipt = _promote_pdf(data, digest(binding), language, draft, rendered, inspect_pdf)
                    drafts[language], receipts[language] = draft, receipt
            except Exception as error:
                editions.setdefault(language, {}).update(state="waiting", reason=_stage_failure(error),
                                                         error_type=type(error).__name__)
        stages["editions"] = editions
        if not writing_complete or set(languages) != set(receipts):
            status.update(state="narration_pending" if not writing_complete else "render_pending", complete=False,
                          failures=result["failures"], job_ids=result["job_ids"])
            codes = {code for rows in result["failures"].values() for code in rows.values()}
            local_failures = ([stages["workbook"].get("reason", "REPORT_LOCAL_STAGE_FAILED")]
                if stages["workbook"]["state"] != "completed" else [])
            local_failures.extend(edition["reason"] for language, edition in editions.items()
                if language in result["complete_languages"] and edition.get("state") != "completed"
                and isinstance(edition.get("reason"), str))
            if not allow_fresh_legacy_model and dataset.get("schema_version") != 3 and local_failures:
                status["reason"] = local_failures[0]
            elif LEGACY_MIGRATION_WAIT in codes and codes <= {LEGACY_MIGRATION_WAIT, "REPORT_SOURCE_ENGLISH_UNAVAILABLE"}:
                status["reason"] = LEGACY_MIGRATION_WAIT
            atomic_json(data / "package-status.json", status)
            return status
        manifest = write_delivery_manifest(issue, drafts, receipts, root=root,
                                           selected_language=choice)
        status.update(state="draft_ready_for_review", complete=manifest["complete"],
                      manifest_path=str(final), package_hash=manifest["package_hash"],
                      job_ids=result["job_ids"], reused=False)
        atomic_json(data / "package-status.json", status)
        return status
