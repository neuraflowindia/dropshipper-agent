"""Frozen actual examples of an accepted functional type, never new products.

This is a saved-only projection. It neither prepares missing memberships nor
matches an unreviewed listing. Original observation and membership records are
retained so a packet reader can check the display without the research store.
An example, a listing and an independent market source are different things.
"""

from copy import deepcopy
import re

from .functional_types import (DEFINITION_FIELDS, _DEFINITION_INTENT_FIELDS,
    _verified_definition, _verify_membership, current_example_memberships,
    effective_functional_definition)
from .models import SafetyError, canonical, digest, identifier, timestamp
from .frozen_records import freeze_original_json, read_original_json
from .public_pages import _public_url
from .research_memory import key


RECOMMENDATION_CONTRACT = "functional-type-examples-v1"
WORKBOOK_HEADERS = ("Rank", "Product type", "Example product", "Source", "Seller", "Pack", "Observed quote",
                    "Observed at", "Source link", "Concept ID", "Example ID", "Membership ID", "Observation ID")
FIELDS = ("recommendation_contract", "functional_definition", "functional_definition_hash",
          "functional_definition_proof", "example_products", "example_bindings",
          "example_as_of", "example_coverage")
FUNCTIONAL_REPORT_FIELDS = FIELDS + (
    "functional_fit_input", "functional_fit_input_hash", "functional_fit_proof", "functional_fit_proof_hash",
    "functional_fit_reference_proofs", "functional_fit_assessment", "fit_description", "functional_market_contract", "functional_market_proof",
    "score_support", "score_support_refs", "support_valid_until", "functional_supplier_proof", "functional_photo_proof",
    "functional_route_proof", "supplier_scoped_terms", "supplier_scope_coverage",
    # Which quick-commerce places were asked for the type (the report's app x city
    # matrix); the market proof itself holds only the listings that were seen.
    "quick_commerce_checks")
OPTIONAL_FUNCTIONAL_REPORT_FIELDS = ("example_review_proofs", "example_selection")
MAX_EXAMPLES = 20
# Directory listings that nominated a type (wider discovery, W8) are supplier
# leads, not market examples: they come after every other example and say so.
SUPPLIER_LISTING_LABELS = {"indiamart": "IndiaMART", "tradeindia": "TradeIndia"}
MAX_BLOCK_BYTES = 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def functional_report_fields(value):
    """Preserve optional new proof blocks without adding keys to old packets."""
    return FUNCTIONAL_REPORT_FIELDS + tuple(name for name in OPTIONAL_FUNCTIONAL_REPORT_FIELDS if name in value)


def example_block_fields(value):
    return FIELDS + tuple(name for name in OPTIONAL_FUNCTIONAL_REPORT_FIELDS if name in value)


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise SafetyError("REPORT_EXAMPLE_HASH_INVALID")
    return value


def _optional_text(value):
    # Structured seller/pack data is not guessed from the marketing title.
    return value if isinstance(value, str) and value.strip() else None


def _url(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise SafetyError("REPORT_EXAMPLE_URL_INVALID")
    _public_url(value)
    return value


def _location(observation):
    facts = observation.get("facts", {})
    # The whole saved location scope is significant; do not merge two delivery
    # areas merely because both belong to the same city.
    return digest({field: facts[field] for field in ("location", "location_context", "reference_location")
                   if field in facts})


def _original_identity(observation):
    source, item = observation.get("source"), observation.get("source_item")
    if not isinstance(source, str) or not source or not isinstance(item, str) or not item:
        raise SafetyError("REPORT_EXAMPLE_OBSERVATION_INVALID")
    return source, item, _location(observation)


def _offer_identity(observation):
    facts = observation["facts"]
    identity = facts.get("asin") if observation["source"] == "amazon" else facts.get("product_id")
    identity = identity if isinstance(identity, str) and identity else observation["source_item"]
    return observation["source"], identity, _location(observation)


def _display(observation):
    if (not isinstance(observation, dict) or not isinstance(observation.get("semantic"), dict)
            or not isinstance(observation.get("facts"), dict)):
        raise SafetyError("REPORT_EXAMPLE_OBSERVATION_INVALID")
    _original_identity(observation)
    for field in ("semantic_id", "capture_id", "acquisition_id"):
        identifier(observation.get(field))
    timestamp(observation.get("observed_at"))
    semantic, facts = observation["semantic"], observation["facts"]
    title = _optional_text(semantic.get("title"))
    if title is None:
        raise SafetyError("REPORT_EXAMPLE_TITLE_MISSING")
    url = next((value for value in (facts.get("url"), semantic.get("product_url"), semantic.get("link_url"))
                if isinstance(value, str) and value), None)
    images = facts.get("image_urls", [])
    if not isinstance(images, list):
        raise SafetyError("REPORT_EXAMPLE_IMAGE_URL_INVALID")
    images = list(images)
    if semantic.get("original_image_url"):
        images.append(semantic["original_image_url"])
    images = sorted(set(_url(value) for value in images if value is not None))
    quote = facts.get("price")
    price = None
    pack = _optional_text(facts.get("quantity"))
    if isinstance(quote, dict):
        # Keep the observed quote; no sale-price inference, pack division,
        # currency guess or advertiser-as-seller inference occurs here.
        displayed = _optional_text(quote.get("displayed"))
        if displayed:
            price = {"kind": "retail", "displayed": displayed,
                     "currency": _optional_text(quote.get("currency")),
                     "basis": _optional_text(quote.get("basis")),
                     "source_url": _url(quote.get("source_url") or url)}
        pack = _optional_text(quote.get("pack_basis")) or pack
    if pack in {"unknown", "not_stated", "not_verified"}:
        pack = None
    seller = _optional_text(facts.get("seller_name")) or _optional_text(facts.get("seller"))
    return {"source": observation["source"], "title": title, "seller": seller,
            "advertiser": _optional_text(facts.get("advertiser_name")),
            "url": _url(url), "observed_at": observation["observed_at"],
            "pack": pack, "price": price, "image_urls": images}


def functional_image_candidates(state, context):
    """Use only original photos of currently type-matched observations.

    This is retrieval, not another identity judgment or a market vote. A
    broader query, an unreviewed newer listing, or an unrelated example cannot
    lend its photograph merely because its title resembles the product type.
    """
    output = {}
    for observation_id, match in context.get("matches", {}).items():
        observation = state.get("observations", {}).get(observation_id)
        if (not isinstance(observation, dict) or match.get("scope") != "narrow_type"
                or match.get("relationship") not in ("exact_product", "relevant_family")):
            continue
        images = list(observation.get("facts", {}).get("image_urls", []))
        original = observation.get("semantic", {}).get("original_image_url")
        if original:
            images.append(original)
        for url in images:
            try:
                clean = _public_url(url)[0]
            except (SafetyError, TypeError, ValueError):
                continue
            output[observation_id, clean] = {"observation_id": observation_id, "url": clean,
                "source": observation["source"], "observed_at": observation["observed_at"],
                "relationship": match["relationship"]}
    return sorted(output.values(), key=lambda row: (row["relationship"] != "exact_product", row["observation_id"], row["url"]))


def freeze_functional_photo(state, provenance):
    """Freeze saved image ownership, never read bytes or authorize a new GET.

    A report-owned copy can survive loss of shared image bytes, but its actual
    source check must survive too. The caller still verifies the owned display
    bytes and the selected observation's current functional match separately.
    """
    records = {}
    if provenance is not None:
        if not isinstance(provenance, dict):
            raise SafetyError("REPORT_FUNCTIONAL_PHOTO_PROOF_INVALID")
        rid = provenance.get("id")
        identifier(rid)
        record = state.get("source_checks", {}).get(rid)
        if not isinstance(record, dict):
            raise SafetyError("REPORT_FUNCTIONAL_PHOTO_PROOF_INVALID")
        records[rid] = freeze_original_json(record)
        shared = record.get("acquisition_ref")
        if shared is not None:
            identifier(shared)
            original = state.get("source_checks", {}).get(shared)
            if not isinstance(original, dict):
                raise SafetyError("REPORT_FUNCTIONAL_PHOTO_PROOF_INVALID")
            records[shared] = freeze_original_json(original)
    return {"contract": "functional-photo-proof-v1", "records": {"source_checks": records}}


def validate_frozen_functional_photo(product):
    """Verify original photo provenance using only the frozen packet.

    This deliberately does not claim that shared or report-owned image bytes
    exist. Byte ownership remains the separate report asset receipt's job.
    """
    from .product_images import (VERSION, SHARED_VERSION, RENEWABLE_VERSION, _candidate,
                                 _shared_record, _renewable_read_record)
    try:
        proof = product["functional_photo_proof"]
        if (set(proof) != {"contract", "records"} or proof["contract"] != "functional-photo-proof-v1"
                or set(proof["records"]) != {"source_checks"}):
            raise SafetyError("REPORT_FUNCTIONAL_PHOTO_PROOF_INVALID")
        records = {rid: read_original_json(value) for rid, value in proof["records"]["source_checks"].items()}
        provenance = product.get("image_provenance")
        if provenance is None:
            if records or product.get("image_path") is not None:
                raise SafetyError("REPORT_FUNCTIONAL_PHOTO_PROOF_INVALID")
            return product
        market = product["functional_market_proof"]
        observation = read_original_json(market["records"]["observations"][provenance["source_ref"]])
        match = market["matches"][provenance["source_ref"]]
        candidate = {"observation_id": provenance["source_ref"], "url": provenance["source_url"],
                     "observed_at": provenance["observed_at"], "relationship": provenance["relationship"]}
        state = {"concepts": {product["concept_id"]: {"description": product["description"]}},
                 "observations": {provenance["source_ref"]: observation}}
        binding = _candidate(state, product["concept_id"], candidate)
        record = records[provenance["id"]]
        if (match["scope"] != "narrow_type" or match["relationship"] != binding["relationship"]
                or record.get("version") not in {VERSION, SHARED_VERSION, RENEWABLE_VERSION}
                or record.get("reference") != provenance["id"]
                or key("product-image", [record["version"], binding,
                    *([record["physical_acquisition_id"]] if record["version"] == RENEWABLE_VERSION else [])]) != provenance["id"]
                or record.get("kind") != "public_product_image" or record.get("source") != "product_photo"
                or record.get("route") != "public_image_http" or record.get("state") != "usable"
                or record.get("model_calls") != 0 or record.get("apify_usd") != "0"
                or any(record.get(field) != value for field, value in binding.items())
                or timestamp(record["at"]) > timestamp(product["example_as_of"])
                or record["receipt"]["checked_at"] != record["at"]
                or record["receipt"].get("state") != "usable"
                or record["receipt"].get("requested_url") != binding["source_url"]):
            raise SafetyError("REPORT_FUNCTIONAL_PHOTO_PROOF_INVALID")
        image = record["image"]
        if (image.get("source_kind") != "public_product_image" or image.get("format") not in {"JPEG", "PNG", "WEBP"}
                or any(type(image.get(field)) is not int or image[field] <= 0 for field in ("width", "height", "bytes"))
                or image["bytes"] > 5 * 1024 * 1024 or not _HASH.fullmatch(image.get("sha256", ""))
                or record["receipt"].get("metadata") != {field: image[field] for field in ("sha256", "format", "width", "height", "bytes")}):
            raise SafetyError("REPORT_FUNCTIONAL_PHOTO_PROOF_INVALID")
        expected_ids = {provenance["id"]}
        if record["version"] in {SHARED_VERSION, RENEWABLE_VERSION}:
            shared = record["acquisition_ref"]
            original = (_renewable_read_record(records[shared], shared, binding["source_url"])
                        if record["version"] == RENEWABLE_VERSION else
                        _shared_record(records[shared], shared, binding["source_url"]))
            expected_ids.add(shared)
            if (record.get("record_hash") != digest({k: v for k, v in record.items() if k != "record_hash"})
                    or original["state"] != "usable" or original["image"] != image
                    or original["receipt"] != record["receipt"] or original["at"] != record["at"]):
                raise SafetyError("REPORT_FUNCTIONAL_PHOTO_PROOF_INVALID")
            if record["version"] == RENEWABLE_VERSION and (
                    record["physical_acquisition_id"] != original["physical_acquisition_id"]
                    or record["generation"] != original["acquisition_binding"]["generation"]
                    or record["next_retry_at"] != original["response_proof"]["image_failure"]["next_retry_at"]
                    or record["failure_classification"] != original["response_proof"]["image_failure"]["classification"]):
                raise SafetyError("REPORT_FUNCTIONAL_PHOTO_PROOF_INVALID")
        expected = {"id": provenance["id"], "owner_id": product["concept_id"], "path": image["path"],
                    "sha256": image["sha256"], "source_kind": "public_product_image",
                    "source_ref": binding["observation_id"], "source_url": binding["source_url"],
                    "observed_at": binding["observed_at"], "relationship": binding["relationship"]}
        if provenance != expected or product.get("image_path") != image["path"] or set(records) != expected_ids:
            raise SafetyError("REPORT_FUNCTIONAL_PHOTO_PROOF_INVALID")
        if "source_revisions" in product and any(product["source_revisions"].get("source_checks", {}).get(rid) != digest(value)
                                                 for rid, value in records.items()):
            raise SafetyError("REPORT_FUNCTIONAL_PHOTO_PROOF_INVALID")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SafetyError("REPORT_FUNCTIONAL_PHOTO_PROOF_INVALID") from None
    return product


def _ordered(candidates):
    """Examples in offer order, every supplier directory listing after the others."""
    return [value[1] for _, value in sorted(candidates.items(),
            key=lambda item: (item[1][1]["source"] in SUPPLIER_LISTING_LABELS, item[0]))]


def freeze_functional_examples(state, concept_id, *, as_of, max_examples=MAX_EXAMPLES):
    """Choose current actual admitted examples without a minimum-count gate.

    Newer global observations of an item supersede old ones even if the newer
    observation has not been admitted to this type. That is not permission to
    resurrect an older conveniently matching seller/title. Source-specific
    list/search context remains intact, then repeated actual offer IDs collapse
    for presentation only. Membership and observation proof are not merged.
    """
    at = timestamp(as_of)
    if type(max_examples) is not int or not 0 <= max_examples <= MAX_EXAMPLES:
        raise SafetyError("REPORT_EXAMPLE_LIMIT_INVALID")
    meaning = effective_functional_definition(state, concept_id)
    if meaning["accepted"] is not True:
        raise SafetyError("REPORT_FUNCTIONAL_DEFINITION_REQUIRED")
    definition = state["source_checks"][meaning["definition_id"]]
    if timestamp(definition["reviewed_at"]) > at:
        raise SafetyError("REPORT_FUNCTIONAL_DEFINITION_AFTER_CUTOFF")
    memberships = current_example_memberships(state, concept_id)
    relevant_items = {(row["source"], row["source_item"]) for member in memberships
                      for row in member["observations"]}
    latest = {}
    for obs_id, observation in state.get("observations", {}).items():
        if (not isinstance(observation, dict)
                or (observation.get("source"), observation.get("source_item")) not in relevant_items):
            continue
        observed = timestamp(observation["observed_at"])
        if observed > at:
            continue
        identity = _original_identity(observation)
        if identity not in latest or (observed, obs_id) > latest[identity][0]:
            latest[identity] = ((observed, obs_id), obs_id)
    candidates, bindings, rejected = {}, {}, []
    for membership in memberships:
        membership_id = membership["membership_id"]
        record = state["source_checks"][membership_id]
        if timestamp(record["prepared_at"]) > at:
            continue
        for captured in membership["observations"]:
            obs_id = captured["observation_id"]
            observation = state["observations"][obs_id]
            if (timestamp(observation["observed_at"]) > at
                    or latest.get(_original_identity(observation), (None, None))[1] != obs_id):
                continue
            try:
                display = _display(observation)
            except SafetyError as error:
                rejected.append({"observation_id": obs_id, "reason": str(error)})
                continue
            row = {"id": key("report-example", [membership_id, obs_id]),
                   "membership_id": membership_id, "membership_hash": record["record_hash"],
                   "child_id": membership["child_id"], "meaning_hash": meaning["meaning_hash"],
                   "observation_id": obs_id, "observation_hash": digest(observation),
                   "observation": freeze_original_json(observation), **display}
            offer = _offer_identity(observation)
            order = (timestamp(observation["observed_at"]), obs_id, membership_id)
            if offer not in candidates or order > candidates[offer][0]:
                candidates[offer] = (order, row)
            bindings[membership_id] = {"membership_hash": record["record_hash"], "record": deepcopy(record)}
    ordered = _ordered(candidates)
    selected = ordered[:max_examples]
    result = {"recommendation_contract": RECOMMENDATION_CONTRACT,
              "functional_definition": deepcopy(meaning), "functional_definition_hash": digest(meaning),
              "functional_definition_proof": {"id": meaning["definition_id"],
                  "record_hash": definition["record_hash"], "record": (
                      freeze_original_json(definition)
                      if "definition_acceptance" in (definition.get("job_proof") or {})
                      else deepcopy(definition))},
              "example_products": selected, "example_bindings": {},
              "example_as_of": as_of, "example_coverage": {
                  "retained_current_offers": len(ordered), "selected_offers": len(selected),
                  "omitted_offers": len(ordered) - len(selected), "limit": max_examples,
                  "rejected_observations": sorted(rejected, key=lambda row: (row["observation_id"], row["reason"]))}}
    def bind_selected(rows):
        result["example_products"] = rows
        result["example_bindings"] = {row["membership_id"]: bindings[row["membership_id"]] for row in rows}
        proofs = {}
        for binding in result["example_bindings"].values():
            ref = binding["record"].get("example_review")
            if ref is not None:
                review = state["source_checks"][ref["id"]]
                from .functional_example_review import validate_membership_example_review
                validate_membership_example_review(binding["record"], review, as_of=as_of)
                proofs[ref["id"]] = freeze_original_json(review)
        if proofs:
            result["example_review_proofs"] = proofs
        else:
            result.pop("example_review_proofs", None)
        coverage = result["example_coverage"]
        coverage.update(selected_offers=len(rows), omitted_offers=len(ordered) - len(rows))

    bind_selected(selected)
    while len(canonical({name: result[name] for name in example_block_fields(result)})) > MAX_BLOCK_BYTES and selected:
        # Several independent original reviews can be large even after shared
        # proofs are stored only once. Keep the type, select fewer actual
        # examples, and explain that file-size limit without inventing facts.
        selected = selected[:-1]
        result["example_coverage"]["limit"] = len(selected)
        result["example_selection"] = {"requested_limit": max_examples, "byte_limit": MAX_BLOCK_BYTES,
            "omitted_for_byte_limit": min(len(ordered), max_examples) - len(selected)}
        bind_selected(selected)
    validate_functional_examples(result, concept_id=concept_id)
    return result


def validate_functional_examples(value, *, concept_id):
    """Check a frozen block without reading mutable research or image files."""
    identifier(concept_id)
    try:
        if (not isinstance(value, dict) or not set(FIELDS) <= set(value)
                or value["recommendation_contract"] != RECOMMENDATION_CONTRACT):
            raise SafetyError("REPORT_EXAMPLE_CONTRACT_INVALID")
        at = timestamp(value["example_as_of"])
        meaning, proof = value["functional_definition"], value["functional_definition_proof"]
        if (not isinstance(proof, dict) or set(proof) != {"id", "record_hash", "record"}
                or not isinstance(meaning, dict)):
            raise SafetyError("REPORT_FUNCTIONAL_DEFINITION_INVALID")
        saved_record = proof["record"]
        wrapped = isinstance(saved_record, dict) and saved_record.get("contract") == "frozen-original-json-v1"
        record = read_original_json(saved_record) if wrapped else saved_record
        definition = _verified_definition(record)
        # Model acceptance retains exact original JSON, including fractional
        # usage and filesystem timestamps larger than JavaScript's safe integer.
        # Old operator/legacy records keep their original plain representation.
        if wrapped != ("definition_acceptance" in (record.get("job_proof") or {})):
            raise SafetyError("REPORT_FUNCTIONAL_DEFINITION_INVALID")
        expected_id = key("functional-definition", {name: record[name] for name in _DEFINITION_INTENT_FIELDS})
        expected = {"concept_id": concept_id, "definition_id": expected_id,
                    "revision": record["revision"], "meaning_hash": record["meaning_hash"],
                    "presentation_hash": record["presentation_hash"], "accepted": True,
                    "basis": "accepted_definition", **deepcopy(definition)}
        if (record["concept_id"] != concept_id or meaning != expected
                or proof["id"] != expected_id or proof["record_hash"] != record["record_hash"]
                or value["functional_definition_hash"] != digest(expected)
                or timestamp(record["reviewed_at"]) > at):
            raise SafetyError("REPORT_FUNCTIONAL_DEFINITION_INVALID")
        coverage, rows, bindings = value["example_coverage"], value["example_products"], value["example_bindings"]
        if (not isinstance(coverage, dict) or set(coverage) != {"retained_current_offers", "selected_offers",
                "omitted_offers", "limit", "rejected_observations"}
                or any(type(coverage[name]) is not int or coverage[name] < 0 for name in
                    ("retained_current_offers", "selected_offers", "omitted_offers", "limit"))
                or coverage["limit"] > MAX_EXAMPLES or not isinstance(rows, list)
                or len(rows) != coverage["selected_offers"] or len(rows) > coverage["limit"]
                or coverage["selected_offers"] != min(coverage["retained_current_offers"], coverage["limit"])
                or coverage["retained_current_offers"] != len(rows) + coverage["omitted_offers"]
                or not isinstance(coverage["rejected_observations"], list)
                or not isinstance(bindings, dict)):
            raise SafetyError("REPORT_EXAMPLE_COVERAGE_INVALID")
        for rejection in coverage["rejected_observations"]:
            if (not isinstance(rejection, dict) or set(rejection) != {"observation_id", "reason"}
                    or not isinstance(rejection["reason"], str)
                    or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,199}", rejection["reason"])):
                raise SafetyError("REPORT_EXAMPLE_COVERAGE_INVALID")
            identifier(rejection["observation_id"])
        if set(bindings) != {row["membership_id"] for row in rows}:
            raise SafetyError("REPORT_EXAMPLE_MEMBERSHIP_INVALID")
        review_proofs = value.get("example_review_proofs", {})
        wanted_reviews = {binding["record"]["example_review"]["id"] for binding in bindings.values()
                          if isinstance(binding, dict) and isinstance(binding.get("record"), dict)
                          and "example_review" in binding["record"]}
        if (not isinstance(review_proofs, dict) or set(review_proofs) != wanted_reviews
                or ("example_review_proofs" in value and not wanted_reviews)):
            raise SafetyError("REPORT_EXAMPLE_MEMBERSHIP_INVALID")
        reviews = {rid: read_original_json(wrapper) for rid, wrapper in review_proofs.items()}
        if "example_selection" in value:
            selection = value["example_selection"]
            if (not isinstance(selection, dict) or set(selection) != {"requested_limit", "byte_limit", "omitted_for_byte_limit"}
                    or any(type(selection[name]) is not int for name in selection)
                    or not coverage["limit"] < selection["requested_limit"] <= MAX_EXAMPLES
                    or selection["byte_limit"] != MAX_BLOCK_BYTES
                    or selection["omitted_for_byte_limit"] != min(coverage["retained_current_offers"], selection["requested_limit"]) - len(rows)
                    or selection["omitted_for_byte_limit"] <= 0):
                raise SafetyError("REPORT_EXAMPLE_COVERAGE_INVALID")
        seen, offers = set(), set()
        for row in rows:
            membership_id = identifier(row["membership_id"])
            binding = bindings[membership_id]
            if not isinstance(binding, dict) or set(binding) != {"membership_hash", "record"}:
                raise SafetyError("REPORT_EXAMPLE_MEMBERSHIP_INVALID")
            member = binding["record"]
            _verify_membership(membership_id, member)
            if "example_review" in member:
                from .functional_example_review import validate_membership_example_review
                review = reviews[member["example_review"]["id"]]
                validate_membership_example_review(member, review, as_of=value["example_as_of"])
                judgment = record["example_decisions"].get(member["child_id"])
                child = review["binding"]["children"][member["child_id"]]
                if (judgment is not None and judgment["association_hash"] == member["association_hash"]
                        and judgment["description_hash"] == child["description_hash"]):
                    raise SafetyError("REPORT_EXAMPLE_MEMBERSHIP_INVALID")
            if (member.get("accepted") is not True or member.get("concept_id") != concept_id
                    or member.get("meaning_hash") != meaning["meaning_hash"]
                    or member.get("meaning_basis") != "accepted_definition"
                    or binding["membership_hash"] != member["record_hash"]
                    or timestamp(member["prepared_at"]) > at):
                raise SafetyError("REPORT_EXAMPLE_MEMBERSHIP_INVALID")
            observation = read_original_json(row["observation"], expected_hash=row["observation_hash"])
            obs_id = identifier(row["observation_id"])
            _hash(row["observation_hash"])
            if (not isinstance(member.get("observation_hashes"), dict)
                    or member["observation_hashes"].get(obs_id) != row["observation_hash"]
                    or row["observation_hash"] != digest(observation)
                    or member.get("semantic_id") != observation.get("semantic_id")
                    or timestamp(observation["observed_at"]) > at):
                raise SafetyError("REPORT_EXAMPLE_OBSERVATION_CHANGED")
            expected_row = {"id": key("report-example", [membership_id, obs_id]),
                "membership_id": membership_id, "membership_hash": member["record_hash"],
                "child_id": member["child_id"], "meaning_hash": meaning["meaning_hash"],
                "observation_id": obs_id, "observation_hash": digest(observation),
                "observation": freeze_original_json(observation), **_display(observation)}
            offer = _offer_identity(observation)
            if row != expected_row or row["id"] in seen or offer in offers:
                raise SafetyError("REPORT_EXAMPLE_DISPLAY_CHANGED")
            seen.add(row["id"]); offers.add(offer)
        if len(canonical({name: value[name] for name in example_block_fields(value)})) > MAX_BLOCK_BYTES:
            raise SafetyError("REPORT_EXAMPLE_BLOCK_SIZE_LIMIT")
    except (KeyError, TypeError, AttributeError, ValueError, RecursionError):
        raise SafetyError("REPORT_EXAMPLE_BLOCK_INVALID") from None
    return value


def example_source_label(row):
    """An example's source as reports show it; a supplier directory listing is a lead."""
    if row["source"] in SUPPLIER_LISTING_LABELS:
        return SUPPLIER_LISTING_LABELS[row["source"]] + " · Supplier listing (lead)"
    return row["source"]


def example_workbook_rows(product):
    """The same factual rows used in rendering and delivery verification."""
    validate_functional_examples(product, concept_id=product["concept_id"])
    return [[product["rank"], product["title"], row["title"], example_source_label(row),
             row["seller"] or "Not stated", row["pack"] or "Not stated",
             (row["price"]["displayed"] + "; unit and price basis not verified") if row["price"] else "Not stated",
             row["observed_at"], row["url"] or "Not stated", product["concept_id"],
             row["child_id"], row["membership_id"], row["observation_id"]]
            for row in product["example_products"]]
