"""Reviewed research directions, not recommendations or current demand evidence."""

from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .discovery import normalise
from .models import SafetyError, identifier, timestamp
from .service import CATEGORIES
from .storage import load_json

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "research" / "catalogue.json"
# About 2.7 KB a heading with indent=1: room for roughly 6,000 headings. The
# daily backup reads public settings files up to 64 MB, so this is the binding bound.
CATALOGUE_LIMIT = 16 * 1024 * 1024


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise SafetyError("CATALOGUE_TEXT_INVALID")


def _texts(value, *, allow_empty=True):
    if not isinstance(value, list) or (not allow_empty and not value):
        raise SafetyError("CATALOGUE_LIST_INVALID")
    for item in value:
        _text(item)


def validate_catalogue(catalogue):
    """Check references and useful distinctions without certifying idea quality."""
    try:
        if catalogue["schema_version"] != 1 or type(catalogue["schema_version"]) is not int:
            raise SafetyError("CATALOGUE_SCHEMA_INVALID")
        _text(catalogue["revision"])
        timestamp(catalogue["built_at"])
        groups = {}
        for name in ("sources", "branches", "families"):
            values = catalogue[name]
            if not isinstance(values, list):
                raise SafetyError("CATALOGUE_LIST_INVALID")
            groups[name] = {}
            for item in values:
                if not isinstance(item, dict):
                    raise SafetyError("CATALOGUE_RECORD_INVALID")
                record_id = identifier(item["id"])
                if record_id in groups[name]:
                    raise SafetyError("CATALOGUE_DUPLICATE_ID")
                groups[name][record_id] = item
        sources, branches, families = (groups[name] for name in ("sources", "branches", "families"))
        if set(branches) & set(families):
            raise SafetyError("CATALOGUE_DUPLICATE_ID")
        for source in sources.values():
            _text(source["title"])
            parsed = urlparse(source["url"])
            sensitive = {"token", "api_key", "apikey", "access_token", "secret", "password", "authorization"}
            if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password
                    or sensitive & {field.casefold() for field in parse_qs(parsed.query)}):
                raise SafetyError("CATALOGUE_SOURCE_INVALID")
            timestamp(source["checked_at"])
            _texts(source["excerpts"], allow_empty=False)
        for branch in branches.values():
            _text(branch["name"])
            if branch["category"] not in CATEGORIES:
                raise SafetyError("CATALOGUE_CATEGORY_INVALID")
            parent = branch["parent_id"]
            if parent is not None and (parent not in branches or branches[parent]["category"] != branch["category"]):
                raise SafetyError("CATALOGUE_PARENT_INVALID")
            seen, cursor = set(), branch
            while cursor is not None:
                if cursor["id"] in seen:
                    raise SafetyError("CATALOGUE_PARENT_CYCLE")
                seen.add(cursor["id"])
                cursor = branches.get(cursor["parent_id"])
        for family in families.values():
            if family["category"] not in CATEGORIES:
                raise SafetyError("CATALOGUE_CATEGORY_INVALID")
            if family["parent_id"] not in branches or branches[family["parent_id"]]["category"] != family["category"]:
                raise SafetyError("CATALOGUE_PARENT_INVALID")
            for field in ("label", "definition", "problem", "rationale", "review_note", "author", "next_review"):
                _text(family[field])
            for field in ("included", "excluded", "aliases", "overlaps", "sources"):
                _texts(family[field], allow_empty=field != "included")
            if family["basis"] not in ("observed", "plausible_extension") or family["disposition"] not in ("usable", "proposed", "retired"):
                raise SafetyError("CATALOGUE_DISPOSITION_INVALID")
            timestamp(family["reviewed_at"])
            if any(ref not in sources for ref in family["sources"]) or (family["basis"] == "observed" and not family["sources"]):
                raise SafetyError("CATALOGUE_SOURCE_REFERENCE_INVALID")
            if any(ref not in families or ref == family["id"] for ref in family["overlaps"]):
                raise SafetyError("CATALOGUE_OVERLAP_INVALID")
            if not isinstance(family["examples"], list) or not family["examples"]:
                raise SafetyError("CATALOGUE_EXAMPLE_INVALID")
            for example in family["examples"]:
                _text(example["label"])
                _texts(example["sources"])
                if example["basis"] not in ("observed", "plausible_extension") or any(ref not in sources for ref in example["sources"]):
                    raise SafetyError("CATALOGUE_EXAMPLE_INVALID")
                if example["basis"] == "observed" and not example["sources"]:
                    raise SafetyError("CATALOGUE_EXAMPLE_INVALID")
            if not isinstance(family["phrases"], list) or not family["phrases"]:
                raise SafetyError("CATALOGUE_PHRASE_INVALID")
            for phrase in family["phrases"]:
                _text(phrase["text"])
                _text(phrase["reason"])
                if (phrase["source"] not in ("meta", "marketplace", "trends", "supplier", "quickcommerce", "dropship") or phrase["purpose"] not in ("discovery", "validation")
                        or phrase["language"] not in ("en", "hi-Latn") or phrase["country"] != "IN"):
                    raise SafetyError("CATALOGUE_PHRASE_INVALID")
            season = family["season"]
            if season["kind"] not in ("evergreen", "seasonal", "unknown") or not isinstance(season["months"], list):
                raise SafetyError("CATALOGUE_SEASON_INVALID")
            if any(type(month) is not int or not 1 <= month <= 12 for month in season["months"]):
                raise SafetyError("CATALOGUE_SEASON_INVALID")
        return catalogue
    except (KeyError, TypeError, AttributeError, ValueError):
        raise SafetyError("CATALOGUE_RECORD_INVALID") from None


def load_catalogue(path=None):
    """The saved catalogue (``DEFAULT_PATH`` is read when called, so tests can
    point it at a frozen copy)."""
    return validate_catalogue(load_json(DEFAULT_PATH if path is None else path, CATALOGUE_LIMIT))


def alias_groups(catalogue):
    """Exact accepted aliases share scheduling history; no fuzzy meaning claim."""
    parents = {family["id"]: family["id"] for family in catalogue["families"]}

    def root(value):
        while parents[value] != value:
            value = parents[value]
        return value

    aliases = {}
    for family in sorted(catalogue["families"], key=lambda item: item["id"]):
        for text in (family["label"], *family["aliases"]):
            alias = normalise(text)
            if alias in aliases:
                a, b = sorted((root(family["id"]), root(aliases[alias])))
                parents[b] = a
            else:
                aliases[alias] = family["id"]
    return {family_id: root(family_id) for family_id in parents}


def catalogue_summary(catalogue, *, unexplored_per_week=8):
    validate_catalogue(catalogue)
    if type(unexplored_per_week) is not int or unexplored_per_week < 1:
        raise SafetyError("CATALOGUE_COVERAGE_INVALID")
    groups = alias_groups(catalogue)
    usable = [family for family in catalogue["families"] if family["disposition"] == "usable"]
    distinct = len({groups[family["id"]] for family in usable})
    counts = Counter(family["category"] for family in usable)
    return {"schema_version": 1, "revision": catalogue["revision"], "families": len(catalogue["families"]),
            "usable_families": len(usable), "distinct_usable_families": distinct,
            "alias_overlap_groups": sorted(sorted(family_id for family_id, group in groups.items() if group == canonical_id)
                                           for canonical_id in set(groups.values())
                                           if sum(group == canonical_id for group in groups.values()) > 1),
            "phrase_variants": len({normalise(phrase["text"]) for family in usable for phrase in family["phrases"]}),
            "source_purpose_options": len({(family["id"], normalise(phrase["text"]), phrase["source"],
                phrase["purpose"], phrase["language"], phrase["country"])
                for family in usable for phrase in family["phrases"]}),
            "observed": sum(family["basis"] == "observed" for family in usable),
            "plausible_extensions": sum(family["basis"] == "plausible_extension" for family in usable),
            "categories": {category: counts[category] for category in CATEGORIES},
            "thin_categories": [category for category in CATEGORIES if counts[category] < 20],
            "rough_weeks_at_assumed_rate": round(distinct / unexplored_per_week, 1),
            "assumed_unexplored_per_week": unexplored_per_week,
            "coverage_note": "Approximate idea inventory, not guaranteed fresh products or an affordable search forecast."}
