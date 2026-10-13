"""Exact public-record bytes inside a portable, integer-only report packet.

Research inputs may contain JSON floating numbers. The existing private portal
deliberately hashes only integers and decimal strings; changing an original
number to a string would invalidate its original proof. This explicit new
packet wrapper keeps that original canonical JSON as text and its original
hash. It is not another research record, source, cache, or authority.
"""

from hashlib import sha256
import re

from .models import SafetyError, canonical, decode_json


CONTRACT = "frozen-original-json-v1"
MAX_BYTES = 1024 * 1024


def freeze_original_json(value):
    try:
        raw = canonical(value)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise SafetyError("FROZEN_ORIGINAL_JSON_INVALID") from None
    if not isinstance(value, dict) or not 1 <= len(raw) <= MAX_BYTES:
        raise SafetyError("FROZEN_ORIGINAL_JSON_SIZE_INVALID")
    return {"contract": CONTRACT, "canonical_json": raw.decode("utf-8"), "sha256": sha256(raw).hexdigest()}


def read_original_json(value, *, expected_hash=None):
    try:
        if (not isinstance(value, dict) or set(value) != {"contract", "canonical_json", "sha256"}
                or value["contract"] != CONTRACT or not isinstance(value["canonical_json"], str)
                or not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])):
            raise SafetyError("FROZEN_ORIGINAL_JSON_INVALID")
        raw = value["canonical_json"].encode("utf-8")
        if not 1 <= len(raw) <= MAX_BYTES:
            raise SafetyError("FROZEN_ORIGINAL_JSON_SIZE_INVALID")
        if sha256(raw).hexdigest() != value["sha256"] or expected_hash not in (None, value["sha256"]):
            raise SafetyError("FROZEN_ORIGINAL_JSON_HASH_MISMATCH")
        result = decode_json(raw)
        if not isinstance(result, dict) or canonical(result) != raw:
            raise SafetyError("FROZEN_ORIGINAL_JSON_INVALID")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise SafetyError("FROZEN_ORIGINAL_JSON_INVALID") from None
    return result
