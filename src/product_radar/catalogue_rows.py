"""Compact heading rows become catalogue headings in the house style.

The product map's headings (catalogue "families") all follow one shape: a
definition and problem written from the heading's purpose, one included
example, its excluded neighbours plus the standard brand/colour/pack sentence,
a Meta discovery phrase and marketplace and Trends validation phrases. Two
callers build headings from a short description:

* the row expander (``scripts/expand_catalogue_rows.py``): a reviewed list of
  compact JSON rows becomes a new ``config/research/catalogue.json`` revision
  (new sub-areas also get their routing words in ``product_map.json``);
* the product map's AI top-up (``map_topup``): a few new headings for one
  sub-area when the map runs low on never-searched headings.

Both write the same wording, so a heading reads the same whichever way it was
added. A heading is a research direction, not demand evidence: its basis is
``plausible_extension`` and it cites no source.
"""

from calendar import monthrange
from datetime import datetime, timezone
import json
import re

from .discovery import normalise
from .models import SafetyError, identifier, timestamp

REVISION = "2026-10-16-r11-map"
AUTHOR = "Claude-assisted exhaustive product map (16 October 2026)"
CATEGORY_CODES = {"hd": "Home and Decor", "hk": "Home and Kitchen", "bb": "Baby Products", "to": "Travel and Outdoor"}
BRANCH_ID = re.compile(r"branch-(hd|hk|bb|to)-[a-z0-9]+(?:-[a-z0-9]+)*")
STANDARD_EXCLUSION = "Brand names, colour choices and pack quantities are not separate search families."
DEFINITION = ("Products whose primary purpose is to {}. Functional mechanisms and audiences are retained; "
              "seller, colour and pack-size changes remain within the family.")
PROBLEM = "People need to {}."
META_REASON = "Look for ads demonstrating how to {}; broad category and offer-only searches would be less focused."
MARKETPLACE_REASON = ("Find comparable listings after a relevant product is identified; supplier availability alone is not "
                      "market evidence. This is an optional route, not an instruction to run every listed source.")
TRENDS_REASON = ("Use only if the identified product matches this meaning; narrow the term to the actual concept rather "
                 "than attributing parent-category interest. This is an optional route, not an instruction to run every "
                 "listed source.")
REVIEW_NOTE = "This catalogue guides research; fresh recommendation evidence and supplier terms are collected later."
BABY_NOTE = ("Baby/child suitability, age, material and product-specific compliance need the existing lightweight "
             "client-review flag. ")
SEASON_NOTE = "Months are a planning judgment, not a measured demand forecast. "
TEXT_LIMIT = 200
PHRASE_LIMIT = 120


def clean_text(value, code, limit=TEXT_LIMIT):
    """One line of plain text (spaces collapsed), or ``SafetyError(code)``: no
    links, no control characters, at most ``limit`` characters."""
    if not isinstance(value, str) or "://" in value or any(ord(char) < 32 for char in value):
        raise SafetyError(code)
    text = " ".join(value.split())
    if not 2 <= len(text) <= limit:
        raise SafetyError(code)
    return text


def purpose_phrase(value):
    """A purpose as the verb phrase the definition needs: "fasten gear to a
    pack" (a leading "to" and a final full stop are dropped; the first letter is
    lower case unless the first word is an abbreviation)."""
    text = clean_text(value, "CATALOGUE_ROW_PURPOSE_INVALID").rstrip(".").strip()
    if text.casefold().startswith("to "):
        text = text[3:].strip()
    if len(text.split()) < 2:  # a verb and what it acts on
        raise SafetyError("CATALOGUE_ROW_PURPOSE_INVALID")
    return text if text[:2].isupper() else text[0].lower() + text[1:]


def season_value(value):
    """``"evergreen"`` or ``{"kind": "seasonal", "months": [...]}`` as the
    catalogue's season shape (months 1-12, sorted, at least one)."""
    if value == "evergreen":
        return {"kind": "evergreen", "months": []}
    if (isinstance(value, dict) and set(value) == {"kind", "months"} and value["kind"] == "seasonal"
            and isinstance(value["months"], list) and value["months"]
            and all(type(month) is int and 1 <= month <= 12 for month in value["months"])
            and len(set(value["months"])) == len(value["months"])):
        return {"kind": "seasonal", "months": sorted(value["months"])}
    raise SafetyError("CATALOGUE_ROW_SEASON_INVALID")


def category_code(category):
    return next(code for code, name in CATEGORY_CODES.items() if name == category)


def family_id(category, label, taken):
    """``<category code>-<label in kebab case>``, at most 80 characters, unique
    against ``taken`` (a number is added on a clash)."""
    words = re.findall(r"[a-z0-9]+", normalise(label).replace("'", ""))
    if not words:
        raise SafetyError("CATALOGUE_ROW_LABEL_INVALID")
    stem = category_code(category) + "-" + "-".join(words)
    if len(stem) > 72:
        stem = stem[:72].rsplit("-", 1)[0]
    candidate, number = stem, 2
    while candidate in taken:
        candidate, number = stem + "-" + str(number), number + 1
    return identifier(candidate)


def next_review(at):
    """The calendar date one month after ``at`` (the house review rhythm)."""
    day = timestamp(at)
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return day.replace(year=year, month=month, day=min(day.day, monthrange(year, month)[1])).date().isoformat()


def review_note(category, season):
    return ((BABY_NOTE if category == "Baby Products" else "")
            + (SEASON_NOTE if season["kind"] == "seasonal" else "") + REVIEW_NOTE)


def rationale(branch, excluded, *, origin):
    """Why the heading exists, said plainly: an assisted research direction,
    not demand evidence or a sourced product."""
    neighbour = next((row for row in excluded if row != STANDARD_EXCLUSION), None)
    return (origin + " research direction for " + branch["name"][0].lower() + branch["name"][1:]
            + "; it widens the product map and is not demand evidence. The example is a search idea, not a sourced "
            "product" + (", and differs from " + neighbour[0].lower() + neighbour[1:] if neighbour else "") + ".")


def heading_family(*, family_id, branch, label, purpose, included, excluded, meta_phrase, trends_phrase=None,
                   season, author, at, origin):
    """One catalogue heading in the house style (see the module text).

    ``purpose`` is a verb phrase ("fasten gear to a pack"); ``included`` and
    ``excluded`` are short example names; the standard exclusion sentence is
    added. ``origin`` starts the rationale ("An AI-assisted")."""
    excluded = [*excluded, STANDARD_EXCLUSION] if STANDARD_EXCLUSION not in excluded else list(excluded)
    plain = label.lower()
    return {"id": family_id, "parent_id": branch["id"], "category": branch["category"], "label": label,
            "definition": DEFINITION.format(purpose), "problem": PROBLEM.format(purpose),
            "included": list(included), "excluded": excluded, "aliases": [], "overlaps": [],
            "examples": [{"label": included[0], "basis": "plausible_extension", "sources": []}],
            "basis": "plausible_extension", "sources": [],
            "rationale": rationale(branch, excluded, origin=origin),
            "phrases": [
                {"text": meta_phrase, "source": "meta", "purpose": "discovery", "language": "en", "country": "IN",
                 "reason": META_REASON.format(purpose)},
                {"text": plain, "source": "marketplace", "purpose": "validation", "language": "en", "country": "IN",
                 "reason": MARKETPLACE_REASON},
                {"text": trends_phrase or plain, "source": "trends", "purpose": "validation", "language": "en",
                 "country": "IN", "reason": TRENDS_REASON}],
            "season": season, "review_note": review_note(branch["category"], season), "disposition": "usable",
            "author": author, "reviewed_at": at, "next_review": next_review(at)}


def known_wordings(catalogue):
    """Every normalised label and alias in the catalogue -> its heading id."""
    return {normalise(text): family["id"] for family in catalogue["families"]
            for text in [family["label"], *family["aliases"]]}


# --- The row expander -------------------------------------------------------


class RowsRefused(SafetyError):
    """The rows cannot become a catalogue; ``problems`` names each one."""

    def __init__(self, problems):
        super().__init__("CATALOGUE_ROWS_REFUSED")
        self.problems = problems


def read_rows(paths):
    """(where, value) for every non-blank line of the JSONL files, in order."""
    rows, problems = [], []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                where = str(path) + ":" + str(number)
                try:
                    rows.append((where, json.loads(line)))
                except json.JSONDecodeError:
                    problems.append(where + ": CATALOGUE_ROW_JSON_INVALID")
    if problems:
        raise RowsRefused(problems)
    return rows


def _routing_words(value):
    if (not isinstance(value, list) or not value or len(value) > 40 or any(
            not isinstance(word, str) or word != " ".join(word.split()) or not 2 <= len(word) <= 40
            or not re.fullmatch(r"[a-z0-9 '-]+", word) for word in value)):
        raise SafetyError("CATALOGUE_ROW_ROUTING_WORDS_INVALID")
    return list(value)


def _new_branch(value, branches):
    if not isinstance(value, dict) or set(value) != {"id", "name", "routing_words"}:
        raise SafetyError("CATALOGUE_ROW_SUB_AREA_INVALID")
    branch_id = value["id"]
    if not isinstance(branch_id, str) or len(branch_id) > 80 or not BRANCH_ID.fullmatch(branch_id):
        raise SafetyError("CATALOGUE_ROW_SUB_AREA_ID_INVALID")
    if branch_id in branches:
        raise SafetyError("CATALOGUE_ROW_SUB_AREA_EXISTS")
    code = branch_id.split("-")[1]
    root = "branch-" + code
    if root not in branches:
        raise SafetyError("CATALOGUE_ROW_SUB_AREA_ID_INVALID")
    branch = {"id": branch_id, "parent_id": root, "category": CATEGORY_CODES[code],
              "name": clean_text(value["name"], "CATALOGUE_ROW_SUB_AREA_NAME_INVALID", 80)}
    return branch, _routing_words(value["routing_words"])


_ROW_FIELDS = {"sub_area_id", "label", "purpose", "included", "excluded", "meta_phrase", "season"}


def _row_texts(row, branches):
    """The row's checked texts (refusals raise ``SafetyError``)."""
    if not isinstance(row, dict) or not _ROW_FIELDS <= set(row) or not set(row) <= _ROW_FIELDS | {"trends_phrase"}:
        raise SafetyError("CATALOGUE_ROW_FIELDS_INVALID")
    branch = branches.get(row["sub_area_id"]) if isinstance(row["sub_area_id"], str) else None
    if branch is None or branch["parent_id"] is None:
        raise SafetyError("CATALOGUE_ROW_SUB_AREA_UNKNOWN")
    for name in ("included", "excluded"):
        if not isinstance(row[name], list) or len(row[name]) > 8 or (name == "included" and not row[name]):
            raise SafetyError("CATALOGUE_ROW_EXAMPLES_INVALID")
    return {"branch": branch, "label": clean_text(row["label"], "CATALOGUE_ROW_LABEL_INVALID", 80),
            "purpose": purpose_phrase(row["purpose"]),
            "included": [clean_text(text, "CATALOGUE_ROW_EXAMPLES_INVALID", 120) for text in row["included"]],
            "excluded": [clean_text(text, "CATALOGUE_ROW_EXAMPLES_INVALID", 120) for text in row["excluded"]],
            "meta_phrase": clean_text(row["meta_phrase"], "CATALOGUE_ROW_PHRASE_INVALID", PHRASE_LIMIT),
            "trends_phrase": (clean_text(row["trends_phrase"], "CATALOGUE_ROW_PHRASE_INVALID", PHRASE_LIMIT)
                              if row.get("trends_phrase") is not None else None),
            "season": season_value(row["season"])}


def excluded_kind_text(texts):
    """Whether any of the texts names one of the client's excluded kinds
    (jewellery, cosmetics, clothing, phone accessories, chargers, stationery,
    pets ...), by the dropshipping platforms' discovery words."""
    from .map_materials import excluded
    return any(excluded(text) for text in texts if text)


def expand_rows(catalogue, settings, rows, *, at, revision=REVISION, author=AUTHOR):
    """The new catalogue and product-map settings from compact rows, with the
    warnings to show; ``RowsRefused`` lists every refused row.

    Refused: an unknown sub-area, a label already used by a heading or alias
    (normalised) or by another row, a row naming an excluded kind, bad fields.
    Warned only: a Meta phrase the client-category gate does not place in the
    row's category, a Meta phrase another heading already uses."""
    from copy import deepcopy
    from .catalogue import validate_catalogue
    from .map_materials import validate_settings
    from .wider_candidates import gate
    timestamp(at)
    catalogue, settings = deepcopy(validate_catalogue(catalogue)), deepcopy(settings)
    branches = {row["id"]: row for row in catalogue["branches"]}
    problems, warnings, added_branches = [], [], []
    for where, value in rows:
        if isinstance(value, dict) and "new_sub_area" in value:
            try:
                if set(value) != {"new_sub_area"}:
                    raise SafetyError("CATALOGUE_ROW_SUB_AREA_INVALID")
                branch, words = _new_branch(value["new_sub_area"], branches)
            except SafetyError as error:
                problems.append(where + ": " + str(error))
                continue
            branches[branch["id"]] = branch
            added_branches.append(branch)
            settings["branch_words"][branch["id"]] = words
    known = known_wordings(catalogue)
    metas = {normalise(phrase["text"]) for family in catalogue["families"] for phrase in family["phrases"]
             if phrase["source"] == "meta"}
    taken = set(branches) | {family["id"] for family in catalogue["families"]}
    families = []
    for where, value in rows:
        if isinstance(value, dict) and "new_sub_area" in value:
            continue
        try:
            row = _row_texts(value, branches)
            wording = normalise(row["label"])
            if wording in known:
                raise SafetyError("CATALOGUE_ROW_LABEL_DUPLICATE")
            if excluded_kind_text([row["label"], row["purpose"], *row["included"], row["meta_phrase"],
                                   row["trends_phrase"]]):
                raise SafetyError("CATALOGUE_ROW_EXCLUDED_KIND")
        except SafetyError as error:
            problems.append(where + ": " + str(error) + (" (" + str(value.get("label")) + ")"
                                                         if isinstance(value, dict) else ""))
            continue
        category = row["branch"]["category"]
        placed = gate(row["meta_phrase"])
        if placed != category:
            warnings.append(where + ": the client-category gate " + ("does not place" if placed is None else
                            "places it in " + placed + ", not " + category + ":") + " Meta phrase '"
                            + row["meta_phrase"] + "'")
        if normalise(row["meta_phrase"]) in metas:
            warnings.append(where + ": another heading already uses the Meta phrase '" + row["meta_phrase"] + "'")
        new_id = family_id(category, row["label"], taken)
        taken.add(new_id)
        known[wording] = new_id
        metas.add(normalise(row["meta_phrase"]))
        families.append(heading_family(
            family_id=new_id, branch=row["branch"], label=row["label"], purpose=row["purpose"],
            included=row["included"], excluded=row["excluded"], meta_phrase=row["meta_phrase"],
            trends_phrase=row["trends_phrase"], season=row["season"], author=author, at=at,
            origin="An AI-assisted"))
    if problems:
        raise RowsRefused(problems)
    for branch in added_branches:
        last = max(index for index, row in enumerate(catalogue["branches"]) if row["category"] == branch["category"])
        catalogue["branches"].insert(last + 1, branch)
    catalogue["families"].extend(families)
    catalogue.update(revision=revision, built_at=at)
    try:
        validate_catalogue(catalogue)
        validate_settings(settings, catalogue=catalogue)
    except SafetyError as error:
        raise RowsRefused(["result: " + str(error)]) from None
    return catalogue, settings, warnings


def encode_catalogue(catalogue):
    """Compact but stable: sorted keys, one space of indent, UTF-8."""
    from .catalogue import CATALOGUE_LIMIT
    encoded = (json.dumps(catalogue, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > CATALOGUE_LIMIT:
        raise RowsRefused(["result: CATALOGUE_TOO_LARGE (" + str(len(encoded)) + " bytes)"])
    return encoded


def encode_settings(settings):
    """``product_map.json`` in its hand-kept layout: one line per setting group
    and one line per sub-area's routing words."""
    lines = []
    for name, value in settings.items():
        if name == "branch_words":
            words = [json.dumps(branch_id) + ": " + json.dumps(rows) for branch_id, rows in value.items()]
            lines.append('  "branch_words": {\n    ' + ",\n    ".join(words) + "\n  }")
        else:
            lines.append("  " + json.dumps(name) + ": " + json.dumps(value))
    return ("{\n" + ",\n".join(lines) + "\n}\n").encode("utf-8")


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
