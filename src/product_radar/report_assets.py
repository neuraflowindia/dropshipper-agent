"""Bounded display copies of genuine photos, owned by one report revision.

No collection, model call, selection, hold or publication happens here. These
helpers prepare the schema3 asset handoff; legacy frozen packets stay unchanged.
Original evidence bytes are never edited. Saved display bytes, not a future
encoder version, are authoritative when recovering an interrupted packet.
"""

from base64 import b64encode
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re

from .models import SafetyError, digest, identifier, timestamp
from .product_images import MEDIA_ROOT, inspect_image
from .public_pages import _public_url
from .research_memory import key
from .runtime_storage import runtime_path
from .storage import Journal, atomic_bytes, atomic_json, load_json, safe_path
from .visual_identity import MAX_IMAGE_BYTES

TRANSFORM = "report-display-image-v1"
PLAN = "report-assets-plan-v1"
RECEIPT = "report-owned-assets-v1"
MAX_DISPLAY_BYTES = 256 * 1024
MAX_EMBEDDED_BYTES = 8 * 1024 * 1024
STEPS = ((1600, 85), (1200, 80), (960, 75))
_EXT = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise SafetyError("REPORT_ASSET_HASH_INVALID")
    return value


def _read(path, limit):
    path = safe_path(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
    except OSError:
        raise SafetyError("REPORT_ASSET_BYTES_MISSING") from None
    if not raw or len(raw) > limit:
        raise SafetyError("REPORT_ASSET_BYTES_LIMIT")
    return raw


def _display_metadata(raw):
    value = inspect_image(raw)
    if value["bytes"] > MAX_DISPLAY_BYTES or max(value["width"], value["height"]) > 1600:
        raise SafetyError("REPORT_DISPLAY_IMAGE_LIMIT")
    return value


def _encoded_bytes(display):
    return len("data:" + _MIME[display["format"]] + ";base64,") + 4 * ((display["bytes"] + 2) // 3)


def _derivative(raw):
    """Three finite, full-frame, metadata-free attempts; never upscale."""
    from PIL import Image, ImageOps, __version__ as pillow_version, features
    original = inspect_image(raw)
    with Image.open(BytesIO(raw)) as opened:
        image = ImageOps.exif_transpose(opened)
        rgba = image.convert("RGBA")
        transparent = rgba.getextrema()[3][0] < 255
        mode = "RGBA" if transparent else "RGB"
        # A fresh image preserves pixels while dropping private EXIF/ICC/text.
        source = Image.frombytes(mode, rgba.convert(mode).size, rgba.convert(mode).tobytes())
    fmt = "WEBP" if transparent and features.check("webp") else "PNG" if transparent else "JPEG"
    for long_edge, quality in STEPS:
        candidate = source.copy()
        candidate.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
        output = BytesIO()
        options = ({"quality": quality, "subsampling": 2, "optimize": True, "progressive": False}
                   if fmt == "JPEG" else {"quality": quality, "method": 6, "exact": True}
                   if fmt == "WEBP" else {"optimize": True})
        candidate.save(output, format=fmt, **options)
        encoded = output.getvalue()
        if len(encoded) <= MAX_DISPLAY_BYTES:
            display = _display_metadata(encoded)
            return {"state": "ready", "source_hash": original["sha256"], "display": display, "reason": None,
                    "transform": {"contract": TRANSFORM, "pillow": pillow_version, "format": fmt,
                                  "long_edge": long_edge, "quality": quality if fmt != "PNG" else None,
                                  "transparent": transparent, "orientation_corrected": True,
                                  "full_frame": True, "upscaled": False}}, encoded
    return {"state": "omitted", "source_hash": original["sha256"], "display": None,
            "transform": None, "reason": "Photo did not fit the report image size allowance."}, None


def _validate_transform(transform, display):
    if (not isinstance(transform, dict) or set(transform) != {"contract", "pillow", "format", "long_edge", "quality",
            "transparent", "orientation_corrected", "full_frame", "upscaled"}
            or transform["contract"] != TRANSFORM or not isinstance(transform["pillow"], str)
            or not 1 <= len(transform["pillow"]) <= 32 or transform["format"] != display["format"]
            or type(transform["long_edge"]) is not int or type(transform["transparent"]) is not bool
            or transform["orientation_corrected"] is not True or transform["full_frame"] is not True
            or transform["upscaled"] is not False
            or (transform["long_edge"], transform["quality"] if display["format"] != "PNG" else
                next((quality for edge, quality in STEPS if edge == transform["long_edge"]), None)) not in STEPS
            or display["format"] == "PNG" and transform["quality"] is not None
            or display["format"] != "PNG" and type(transform["quality"]) is not int
            or max(display["width"], display["height"]) > transform["long_edge"]
            or (display["format"] != "JPEG") != transform["transparent"]):
        raise SafetyError("REPORT_DISPLAY_TRANSFORM_INVALID")


def _validate_stage(saved, source_hash):
    if (not isinstance(saved, dict) or set(saved) != {"contract", "record_hash", "source_hash", "state", "display", "transform", "reason"}
            or saved.get("record_hash") != digest({name: value for name, value in saved.items() if name != "record_hash"})
            or saved.get("contract") != TRANSFORM or saved.get("source_hash") != source_hash
            or saved.get("state") not in {"ready", "omitted"}):
        raise SafetyError("REPORT_DISPLAY_RECEIPT_CHANGED")
    if saved["state"] == "ready":
        display = saved["display"]
        if (not isinstance(display, dict) or set(display) != {"sha256", "format", "width", "height", "bytes"}
                or display["format"] not in _EXT or any(type(display[field]) is not int or display[field] <= 0
                    for field in ("width", "height", "bytes"))
                or max(display["width"], display["height"]) > 1600 or display["bytes"] > MAX_DISPLAY_BYTES
                or saved["reason"] is not None):
            raise SafetyError("REPORT_DISPLAY_RECEIPT_CHANGED")
        _hash(display["sha256"]); _validate_transform(saved["transform"], display)
    elif (saved["display"] is not None or saved["transform"] is not None
            or not isinstance(saved["reason"], str) or not 1 <= len(saved["reason"]) <= 300):
        raise SafetyError("REPORT_DISPLAY_RECEIPT_CHANGED")
    return saved


def _stage_display(staging_root, source_hash, read_source):
    """Reuse exact prior derivative bytes before reading mutable source paths."""
    source_hash = _hash(source_hash)
    folder = safe_path(staging_root, key("display-source", [TRANSFORM, source_hash]))
    with Journal(folder / "writer").locked():
        saved_path = safe_path(folder / "display.json")
        if saved_path.exists():
            saved = load_json(saved_path, 16384)
            _validate_stage(saved, source_hash)
            if saved["state"] == "ready":
                raw = _read(folder / (saved["display"]["sha256"] + _EXT[saved["display"]["format"]]), MAX_DISPLAY_BYTES)
                if _display_metadata(raw) != saved["display"]:
                    raise SafetyError("REPORT_DISPLAY_BYTES_CHANGED")
            return saved
        original = read_source()
        if sha256(original).hexdigest() != source_hash:
            raise SafetyError("REPORT_ASSET_SOURCE_CHANGED")
        saved, raw = _derivative(original)
        saved["contract"] = TRANSFORM
        saved["record_hash"] = digest(saved)
        if raw is not None:
            atomic_bytes(folder / (saved["display"]["sha256"] + _EXT[saved["display"]["format"]]), raw)
        atomic_json(saved_path, saved)
        return saved


def _photo_source(product, media_root):
    photo = deepcopy(product.get("image_provenance"))
    if photo is None:
        return None
    if (not isinstance(photo, dict) or photo.get("owner_id") != product["concept_id"]
            or photo.get("source_kind") != "public_product_image"
            or photo.get("relationship") not in {"exact_product", "relevant_family"}):
        raise SafetyError("REPORT_ASSET_SOURCE_BINDING_INVALID")
    for field in ("id", "owner_id", "source_ref"):
        identifier(photo.get(field))
    _hash(photo.get("sha256")); timestamp(photo.get("observed_at")); _public_url(photo.get("source_url"))
    path = safe_path(photo.get("path"))
    if not path.is_relative_to(safe_path(media_root)):
        raise SafetyError("REPORT_ASSET_SOURCE_OUTSIDE_MEDIA")
    return photo


def prepare_report_asset_plan(product_facts, *, staging_root, media_root=None, logo_path=None,
                              encoded_budget=MAX_EMBEDDED_BYTES):
    """Content-based plan before revision/hold selection; optional loss is local.

    The caller supplies checked original provenance. Plan identity excludes
    filesystem paths, issue/revision, receipt hash and final packet hashes.
    Every product occurrence counts even if several use the same photo bytes.
    """
    if (not isinstance(product_facts, list) or len(product_facts) > 20
            or type(encoded_budget) is not int or not 0 <= encoded_budget <= MAX_EMBEDDED_BYTES):
        raise SafetyError("REPORT_ASSET_PLAN_INPUT_INVALID")
    staging_root = safe_path(staging_root)
    media_root = safe_path(media_root or MEDIA_ROOT)
    owners = [identifier(row.get("concept_id")) for row in product_facts if isinstance(row, dict)]
    if len(owners) != len(product_facts) or len(set(owners)) != len(owners):
        raise SafetyError("REPORT_ASSET_PLAN_INPUT_INVALID")
    inputs = []
    if logo_path is not None:
        # The configured brand logo is not a product photograph or market fact.
        try:
            path = safe_path(logo_path)
            raw = _read(path, MAX_IMAGE_BYTES)
            source = {"kind": "brand_logo", "owner_id": "brand-logo", "sha256": inspect_image(raw)["sha256"]}
            inputs.append((source, lambda raw=raw: raw))
        except (SafetyError, OSError):
            inputs.append(({"kind": "brand_logo", "owner_id": "brand-logo"}, None))
    for product in product_facts:
        source = {"kind": "product_photo", "owner_id": product["concept_id"]}
        try:
            photo = _photo_source(product, media_root)
            if photo is not None:
                source.update({name: photo[name] for name in
                    ("id", "source_ref", "sha256", "source_url", "observed_at", "relationship")})
                inputs.append((source, lambda path=photo["path"]: _read(path, MAX_IMAGE_BYTES)))
            else:
                inputs.append((source, None))
        except (SafetyError, OSError, TypeError, KeyError):
            inputs.append((source, None))
    entries, total = [], 0
    for source, reader in inputs:
        entry = {"asset_id": key("report-asset", [source["kind"], source["owner_id"]]), "source": source,
                 "display": None, "transform": None, "state": "omitted", "reason": "Product photo was not saved.",
                 "failure_code": "REPORT_PHOTO_NOT_SAVED"}
        if source["kind"] == "brand_logo":
            entry["reason"] = "Brand image was not saved."
        if reader is not None:
            try:
                staged = _stage_display(staging_root, source["sha256"], reader)
                if staged["state"] == "ready":
                    size = _encoded_bytes(staged["display"])
                    if total + size <= encoded_budget:
                        entry.update(state="ready", display=staged["display"], transform=staged["transform"], reason=None,
                                     failure_code=None)
                        total += size
                    else:
                        entry["reason"] = "Photo did not fit the report image size allowance."
                        entry["failure_code"] = "REPORT_ASSET_ENCODED_LIMIT"
                else:
                    entry["reason"] = staged["reason"]
                    entry["failure_code"] = "REPORT_DISPLAY_IMAGE_LIMIT"
            except (SafetyError, OSError, KeyError, TypeError, ValueError) as error:
                entry["reason"] = "Saved photo could not be prepared for this report."
                code = str(error) if isinstance(error, SafetyError) else "REPORT_DISPLAY_LOCAL_FAILED"
                entry["failure_code"] = code if re.fullmatch(r"[A-Z][A-Z0-9_]{1,199}", code) else "REPORT_DISPLAY_LOCAL_FAILED"
        entries.append(entry)
    plan = {"contract": PLAN, "transform_contract": TRANSFORM, "encoded_budget": encoded_budget,
            "embedded_bytes": total, "entries": entries}
    return {"plan": plan, "plan_hash": digest(plan), "staging_root": str(staging_root)}


def _validate_plan(plan):
    if (not isinstance(plan, dict) or set(plan) != {"contract", "transform_contract", "encoded_budget", "embedded_bytes", "entries"}
            or plan["contract"] != PLAN or plan["transform_contract"] != TRANSFORM
            or type(plan["encoded_budget"]) is not int or not 0 <= plan["encoded_budget"] <= MAX_EMBEDDED_BYTES
            or not isinstance(plan["entries"], list) or len(plan["entries"]) > 21):
        raise SafetyError("REPORT_ASSET_PLAN_INVALID")
    seen, total, products, logos = set(), 0, 0, 0
    for row in plan["entries"]:
        if not isinstance(row, dict) or set(row) != {"asset_id", "source", "display", "transform", "state", "reason", "failure_code"}:
            raise SafetyError("REPORT_ASSET_PLAN_INVALID")
        source = row["source"]
        if (not isinstance(source, dict) or source.get("kind") not in {"product_photo", "brand_logo"}
                or row["asset_id"] != key("report-asset", [source["kind"], identifier(source.get("owner_id"))])
                or row["asset_id"] in seen or row["state"] not in {"ready", "omitted"}):
            raise SafetyError("REPORT_ASSET_PLAN_INVALID")
        seen.add(row["asset_id"])
        products += source["kind"] == "product_photo"
        logos += source["kind"] == "brand_logo"
        if products > 20 or logos > 1 or source["kind"] == "brand_logo" and source["owner_id"] != "brand-logo":
            raise SafetyError("REPORT_ASSET_PLAN_INVALID")
        if row["state"] == "ready":
            display, transform = row["display"], row["transform"]
            _hash(source.get("sha256"))
            if (not isinstance(display, dict) or set(display) != {"sha256", "format", "width", "height", "bytes"}
                    or display["format"] not in _EXT or any(type(display[field]) is not int or display[field] <= 0
                        for field in ("width", "height", "bytes"))
                    or max(display["width"], display["height"]) > 1600 or display["bytes"] > MAX_DISPLAY_BYTES
                    or not isinstance(transform, dict) or transform.get("contract") != TRANSFORM
                    or row["reason"] is not None or row["failure_code"] is not None):
                raise SafetyError("REPORT_ASSET_PLAN_INVALID")
            _hash(display["sha256"])
            if source["kind"] == "product_photo":
                if (set(source) != {"kind", "owner_id", "id", "source_ref", "sha256", "source_url", "observed_at", "relationship"}
                        or source["relationship"] not in {"exact_product", "relevant_family"}):
                    raise SafetyError("REPORT_ASSET_PLAN_INVALID")
                identifier(source["id"]); identifier(source["source_ref"])
                timestamp(source["observed_at"]); _public_url(source["source_url"])
            elif set(source) != {"kind", "owner_id", "sha256"}:
                raise SafetyError("REPORT_ASSET_PLAN_INVALID")
            _validate_transform(transform, display)
            total += _encoded_bytes(display)
        elif (row["display"] is not None or row["transform"] is not None or not isinstance(row["reason"], str)
                or not 1 <= len(row["reason"]) <= 300 or not isinstance(row["failure_code"], str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,199}", row["failure_code"])):
            raise SafetyError("REPORT_ASSET_PLAN_INVALID")
    if type(plan["embedded_bytes"]) is not int or total != plan["embedded_bytes"] or total > plan["encoded_budget"]:
        raise SafetyError("REPORT_ASSET_ENCODED_LIMIT")
    return plan


def _validate_owner(issue_id, revision):
    identifier(issue_id)
    if type(revision) is not int or not 1 <= revision <= 9007199254740991:
        raise SafetyError("REPORT_ASSET_OWNER_INVALID")


def _asset_folder(root, issue_id, revision):
    _validate_owner(issue_id, revision)
    return safe_path(runtime_path(root, "media", "live", "reports"), issue_id, "revision-" + str(revision), "assets")


def _staged_asset(bundle, row):
    display = row["display"]
    stage = safe_path(bundle["staging_root"], key("display-source", [TRANSFORM, row["source"]["sha256"]]))
    staged = _validate_stage(load_json(stage / "display.json", 16384), row["source"]["sha256"])
    if staged["display"] != display or staged["transform"] != row["transform"]:
        raise SafetyError("REPORT_DISPLAY_RECEIPT_CHANGED")
    raw = _read(stage / (display["sha256"] + _EXT[display["format"]]), MAX_DISPLAY_BYTES)
    if _display_metadata(raw) != display:
        raise SafetyError("REPORT_DISPLAY_BYTES_CHANGED")
    return raw


def freeze_report_assets(bundle, *, root, issue_id, revision):
    """Copy checked derivatives into one immutable owner; resume exact bytes."""
    if not isinstance(bundle, dict) or set(bundle) != {"plan", "plan_hash", "staging_root"}:
        raise SafetyError("REPORT_ASSET_PLAN_INVALID")
    plan = _validate_plan(bundle.get("plan"))
    if bundle.get("plan_hash") != digest(plan):
        raise SafetyError("REPORT_ASSET_PLAN_CHANGED")
    folder = _asset_folder(root, issue_id, revision)
    receipt = {"contract": RECEIPT, "issue_id": issue_id, "revision": revision,
               "plan_hash": bundle["plan_hash"], "plan": deepcopy(plan)}
    receipt["record_hash"] = digest(receipt)
    with Journal(folder / "writer").locked():
        receipt_path = safe_path(folder / "receipt.json")
        if receipt_path.exists() and load_json(receipt_path, 256 * 1024) != receipt:
            raise SafetyError("REPORT_ASSET_FROZEN_INPUT_CHANGED")
        for row in plan["entries"]:
            if row["state"] != "ready":
                continue
            display = row["display"]
            name = display["sha256"] + _EXT[display["format"]]
            destination = safe_path(folder / name)
            damaged = False
            if destination.exists():
                try:
                    raw = _read(destination, MAX_DISPLAY_BYTES)
                    damaged = _display_metadata(raw) != display
                except (SafetyError, OSError):
                    damaged = True
            else:
                raw = _staged_asset(bundle, row)
                atomic_bytes(destination, raw)
            if damaged:
                # Restore only the exact previously frozen hash. This is byte
                # repair, never a new image, transform or changed packet fact.
                try:
                    raw = _staged_asset(bundle, row)
                except (SafetyError, OSError, KeyError, TypeError):
                    raise SafetyError("REPORT_ASSET_EXACT_RECOVERY_MISSING") from None
                atomic_bytes(destination, raw)
            if _display_metadata(raw) != display:
                raise SafetyError("REPORT_ASSET_OWNED_BYTES_CHANGED")
        if not receipt_path.exists():
            atomic_json(receipt_path, receipt)
    return receipt


def validate_report_asset_receipt(receipt, *, issue_id, revision):
    """Pure receipt validation shared by rendering and exact-byte backup.

    This does not inspect a directory or imply that its declared bytes exist.
    The caller must separately check the original owned path and byte hashes.
    """
    _validate_owner(issue_id, revision)
    try:
        if (not isinstance(receipt, dict) or set(receipt) != {"contract", "issue_id", "revision", "plan_hash", "plan", "record_hash"}
                or receipt.get("contract") != RECEIPT or receipt.get("issue_id") != issue_id
                or type(receipt.get("revision")) is not int or receipt["revision"] != revision
                or receipt["record_hash"] != digest({k: v for k, v in receipt.items() if k != "record_hash"})
                or receipt["plan_hash"] != digest(_validate_plan(receipt["plan"]))):
            raise SafetyError("REPORT_ASSET_RECEIPT_INVALID")
    except (TypeError, KeyError, ValueError, AttributeError, RecursionError):
        raise SafetyError("REPORT_ASSET_RECEIPT_INVALID") from None
    return deepcopy(receipt)


def resolve_report_asset(receipt, asset_id, *, root, issue_id, revision):
    """Only this exact revision's declared display bytes can be embedded."""
    folder = _asset_folder(root, issue_id, revision)
    receipt = validate_report_asset_receipt(receipt, issue_id=issue_id, revision=revision)
    if load_json(folder / "receipt.json", 256 * 1024) != receipt:
        raise SafetyError("REPORT_ASSET_RECEIPT_INVALID")
    row = next((row for row in receipt["plan"]["entries"] if row["asset_id"] == asset_id), None)
    if row is None or row["state"] != "ready":
        raise SafetyError("REPORT_ASSET_NOT_DECLARED")
    display = row["display"]
    raw = _read(folder / (display["sha256"] + _EXT[display["format"]]), MAX_DISPLAY_BYTES)
    if _display_metadata(raw) != display:
        raise SafetyError("REPORT_ASSET_OWNED_BYTES_CHANGED")
    return {"bytes": raw, "data_url": "data:" + _MIME[display["format"]] + ";base64," + b64encode(raw).decode("ascii"),
            "display_asset": {"asset_id": asset_id, "owned_relative_ref": "assets/" + display["sha256"] + _EXT[display["format"]],
                "output_sha256": display["sha256"], "bytes": display["bytes"], "source_hash": row["source"]["sha256"],
                "transform_contract": TRANSFORM, "asset_receipt_hash": digest(receipt)}}


def report_display_asset(receipt, asset_id, *, issue_id, revision):
    """Pure declared display reference; actual bytes are verified separately."""
    receipt = validate_report_asset_receipt(receipt, issue_id=issue_id, revision=revision)
    row = next((row for row in receipt["plan"]["entries"] if row["asset_id"] == asset_id), None)
    if row is None:
        raise SafetyError("REPORT_ASSET_NOT_DECLARED")
    if row["state"] == "omitted":
        return None
    display = row["display"]
    return {"asset_id": asset_id, "owned_relative_ref": "assets/" + display["sha256"] + _EXT[display["format"]],
            "output_sha256": display["sha256"], "bytes": display["bytes"],
            "source_hash": row["source"]["sha256"], "transform_contract": TRANSFORM,
            "asset_receipt_hash": digest(receipt)}


def owned_plan_matches(receipt, product_facts, *, media_root=None, logo_path=None):
    """Match original provenance before consulting possibly lost source bytes.

    This is not byte validation or permission to adopt another photograph.
    A matching plan must still pass attach_report_assets with its owned files.
    """
    entries = receipt["plan"]["entries"]
    expected = []
    if logo_path is not None:
        source = {"kind": "brand_logo", "owner_id": "brand-logo"}
        try:
            source["sha256"] = inspect_image(_read(safe_path(logo_path), MAX_IMAGE_BYTES))["sha256"]
        except (SafetyError, OSError):
            pass
        expected.append(source)
    for product in product_facts:
        source = {"kind": "product_photo", "owner_id": product["concept_id"]}
        try:
            photo = _photo_source(product, media_root or MEDIA_ROOT)
        except (SafetyError, OSError, TypeError, KeyError):
            return False
        if photo is not None:
            source.update({name: photo[name] for name in
                ("id", "source_ref", "sha256", "source_url", "observed_at", "relationship")})
        expected.append(source)
    return ([row["source"] for row in entries] == expected
            and receipt["plan"]["transform_contract"] == TRANSFORM
            and receipt["plan"]["encoded_budget"] == MAX_EMBEDDED_BYTES)


def validate_packet_assets(issue, products, receipt, *, root=None):
    """One exact issue owns the declared photos/logo, not mutable source paths.

    With root=None this checks only the frozen contract, suitable for an
    independently downloaded factual packet. Local rendering/backup supplies
    the root and additionally reads every declared ready original owned copy.
    """
    receipt = validate_report_asset_receipt(receipt, issue_id=issue["issue_id"], revision=issue["revision"])
    if not isinstance(products, list) or any(not isinstance(row, dict) for row in products):
        raise SafetyError("REPORT_PACKET_ASSETS_INVALID")
    ids = [identifier(row.get("concept_id")) for row in products]
    entries = receipt["plan"]["entries"]
    photos = {row["source"]["owner_id"]: row for row in entries if row["source"]["kind"] == "product_photo"}
    if len(set(ids)) != len(ids) or set(photos) != set(ids):
        raise SafetyError("REPORT_PACKET_ASSETS_INVALID")
    for product in products:
        row = photos[product["concept_id"]]
        expected = report_display_asset(receipt, row["asset_id"], issue_id=issue["issue_id"], revision=issue["revision"])
        if "display_asset" not in product or product["display_asset"] != expected:
            raise SafetyError("REPORT_PACKET_ASSET_BINDING_CHANGED")
        if row["state"] == "ready":
            original = product.get("image_provenance")
            if (not isinstance(original, dict) or original.get("owner_id") != product["concept_id"]
                    or original.get("source_kind") != "public_product_image"
                    or any(original.get(name) != row["source"].get(name) for name in
                           ("id", "source_ref", "sha256", "source_url", "observed_at", "relationship"))):
                raise SafetyError("REPORT_PACKET_ASSET_SOURCE_CHANGED")
    if root is not None:
        for row in entries:
            if row["state"] == "ready":
                resolve_report_asset(receipt, row["asset_id"], root=root,
                                     issue_id=issue["issue_id"], revision=issue["revision"])
    return receipt


def attach_report_assets(issue, product_facts, receipt, *, root):
    """Bind checked owned copies before final selection/hold; never reserve.

    Optional omissions have already been decided in the content-based plan.
    This does not repair an already frozen packet or change original provenance.
    """
    from .report_examples import RECOMMENDATION_CONTRACT
    from .issue_allocation import _fingerprint, selection_fingerprint
    if issue.get("recommendation_contract") != RECOMMENDATION_CONTRACT:
        raise SafetyError("REPORT_PACKET_ASSET_CONTRACT_REQUIRED")
    frozen, facts = deepcopy(issue), deepcopy(product_facts)
    if (not isinstance(facts, list) or any(not isinstance(row, dict) for row in facts)
            or {row.get("concept_id") for row in facts} != {row["concept_id"] for row in frozen["products"]}
            or len(facts) != len(frozen["products"])):
        raise SafetyError("REPORT_PACKET_ASSETS_INVALID")
    receipt = validate_report_asset_receipt(receipt, issue_id=issue["issue_id"], revision=issue["revision"])
    by_owner = {row["source"]["owner_id"]: row for row in receipt["plan"]["entries"]
                if row["source"]["kind"] == "product_photo"}
    for product in [*frozen["products"], *facts]:
        row = by_owner.get(product["concept_id"])
        if row is None:
            raise SafetyError("REPORT_PACKET_ASSETS_INVALID")
        expected = report_display_asset(receipt, row["asset_id"], issue_id=issue["issue_id"], revision=issue["revision"])
        if "display_asset" in product and product["display_asset"] != expected:
            raise SafetyError("REPORT_PACKET_ASSET_BINDING_CHANGED")
        product["display_asset"] = expected
    validate_packet_assets(frozen, frozen["products"], receipt, root=root)
    validate_packet_assets(frozen, facts, receipt, root=root)
    frozen["report_asset_receipt"] = deepcopy(receipt)
    frozen["evidence_fingerprint"] = _fingerprint(frozen["products"])
    frozen["selection_hash"] = selection_fingerprint(frozen)
    return frozen, facts
