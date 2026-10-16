"""Assemble one report edition: product views from checked facts, then the pages.

``live_outputs.render_html`` validates the packet and resolves the owned
images, then calls ``render_document``. Every value shown comes from the
frozen facts, the code-owned score and wording, or the accepted narrative.
"""

from html import escape

from . import report_pages as pages
from .report_copy import TEXT, date_window, long_date, short_date
from .report_theme import stylesheet
from .report_visuals import (compliance_classes, market_context, meta_view, platform_view, qc_view, score_view,
                             trends_view)

TERM_FIELDS = {"cod": "cod", "plain": "neutral_packaging", "label": "white_label"}


def _fit(product, prose, total_default):
    from .fit_policy import FIT_POLICY_ID
    rows = []
    if product.get("fit_policy_id") == FIT_POLICY_ID:
        from .research_ai import CRITERIA_BY_ID
        phrases = {row["id"]: row["text"] for row in prose.get("checklist_phrases", [])}
        rows = [{"name": CRITERIA_BY_ID[row["id"]], "state": row["state"], "estimated": row["basis"] == "inferred",
                 "phrase": phrases.get(row["id"], "")} for row in product["criteria"]]
    return {"rows": rows, "states": [row["state"] for row in rows], "matches": product["kpi_matches"],
            "total": product.get("kpi_total", total_default), "tier": product.get("fit_tier") if rows else None}


def _price_rows(product, language):
    from .live_outputs import price_display
    retail, supplier = [], []
    for price in product["prices"]:
        head, _, rest = price_display(price, language).partition("; ")
        figure = head.split(": ", 1)[1] if ": " in head else head
        if price.get("amount") is not None:
            figure = pages.rupees(price["amount"])
        source = " · ".join(part for part in (pages.host(price.get("source_url")),
                            TEXT[language]["seen"].format(d=short_date(price["observed_at"])) if price.get("observed_at") else None) if part)
        (retail if price["kind"] == "retail" else supplier).append((figure, rest, source, None))
    return retail, supplier


def _examples(product, language):
    from .report_examples import SUPPLIER_LISTING_LABELS, example_source_label
    words = TEXT[language]
    rows = product["example_products"]
    shown = rows[:pages.SHOWN_EXAMPLES]
    shops, leads = [], []
    for row in shown:
        figure = row["price"]["displayed"] if row["price"] else words["no_quote"]
        label = example_source_label(row) if row["source"] in SUPPLIER_LISTING_LABELS else pages.source_name(row["source"])
        source = " · ".join(part for part in (label, row["seller"], row["pack"] and words["ex_pack"].format(p=row["pack"]),
                                               words["seen"].format(d=short_date(row["observed_at"]))) if part)
        (leads if row["source"] in SUPPLIER_LISTING_LABELS else shops).append((figure, row["title"], source, row["url"]))
    retained = product.get("example_coverage", {}).get("omitted_offers", 0)
    notes = [words["ex_retained"].format(n=retained)] if retained else []
    if product.get("example_selection") and shown:
        notes.append(words["ex_fewer"])
    return {"rows": shown, "shops": shops, "leads": leads, "more": len(rows) - len(shown),
            "omitted_for_size": bool(product.get("example_selection")), "retained": retained, "notes": notes}


def _term(supplier, field):
    rows = [row for row in supplier.get("terms") or [] if isinstance(row, dict) and row.get("field") == field]
    if len(rows) == 1 and rows[0].get("basis") == "observed" and rows[0].get("value") in ("Yes", "No"):
        return rows[0]["value"].lower()
    if any(row.get("basis") == "conflicting" for row in rows):
        return "conflicting"
    return "not_stated"


def _subscription(supplier, terms_version):
    from .live_outputs import SUPPLIER_TERMS_DISPLAY_VERSION
    value = {"no_subscription_stated": "no", "subscription_stated": "yes"}.get(supplier["subscription"], "not_stated")
    if value == "not_stated" and terms_version == SUPPLIER_TERMS_DISPLAY_VERSION:
        rows = [row for row in supplier.get("terms") or [] if isinstance(row, dict) and row.get("field") == "subscription_requirement"]
        if len(rows) == 1 and rows[0].get("basis") == "observed" and rows[0].get("value") == "Optional":
            value = "optional"
        elif len(rows) == 1 and rows[0].get("basis") == "conflicting" and rows[0].get("value") == "Conflicting":
            value = "conflicting"
    return value


def _suppliers(product, language):
    from .fit_policy import FIT_POLICY_ID
    from .live_outputs import (SUPPLIER_TERMS_DISPLAY_VERSION, _supplier_minimum_order, _supplier_terms_version,
                               supplier_terms_display)
    from .report_source_facts import CONTRACTS as SOURCE_ROUTE_CONTRACTS
    from .supplier_scoped_display import is_scoped_supplier, scoped_supplier_lines
    words = TEXT[language]
    every = product.get("source_route_contract") in SOURCE_ROUTE_CONTRACTS
    shown = product["suppliers"] if every else product["suppliers"][:2]
    terms_version = _supplier_terms_version(product)
    shared = product.get("supplier_limitation")
    rows = []
    for supplier in shown:
        seen = supplier.get("source_capture_at") or supplier.get("reviewed_at")
        reason = supplier.get("reason") or ""
        details = [words["lead_seen"].format(d=short_date(seen)) if isinstance(seen, str) else None]
        if reason and reason not in (supplier.get("type_scope_limitation"), shared):
            details.append(pages.fixed_reason(reason, language))
        row = {"name": supplier["name"], "url": supplier.get("url"), "details": details, "scoped": None}
        if is_scoped_supplier(supplier):
            row["scoped"] = scoped_supplier_lines(supplier, language)
            row["links"], seen_pages = [], set()
            for scope in supplier["company_workflows"] + supplier["product_offers"]:
                for page in scope["source_pages"]:
                    if page["url"] not in seen_pages:
                        seen_pages.add(page["url"])
                        kind = words["offer_source"] if scope["scope_kind"] == "product_offer" else words["company_source"]
                        row["links"].append((kind + ": " + scope["scope_label"], page["url"]))
        else:
            text = supplier_terms_display(supplier, language, terms_version=terms_version)
            # Nothing stated beyond the table's own "not stated" cells: no extra line.
            row["terms_text"] = text if text != supplier_terms_display({"terms": []}, language, terms_version=terms_version) else ""
            moq = _supplier_minimum_order(supplier)
            row["moq"] = words["not_stated"] if moq == "Not stated" else str(moq)
            row["terms"] = {"dispatch": {"supported": "yes", "not_supported": "no"}.get(supplier["single_piece_dispatch"], "not_stated"),
                            **{column: _term(supplier, field) for column, field in TERM_FIELDS.items()},
                            "fees": _subscription(supplier, terms_version)}
        rows.append(row)
    notes = []
    if shown and isinstance(shared, str) and shared.strip():
        notes.append(pages.fixed_reason(shared, language))
    if len(product["suppliers"]) > len(shown):
        notes.append(words["sup_more"].format(n=len(product["suppliers"]) - len(shown)))
    status = product["supplier_search_status"]
    empty = (words["supplier_not_started"] if status == "not_started" else words["supplier_blocked"] if status == "blocked"
             else words["missing_supplier"])
    if product.get("fit_policy_id") == FIT_POLICY_ID and product.get("supplier_effort_satisfied") is True:
        empty = words["supplier_no_results"]
    columns = pages.TERM_COLUMNS if terms_version == SUPPLIER_TERMS_DISPLAY_VERSION else pages.LEGACY_TERM_COLUMNS
    return {"rows": rows, "notes": notes, "empty": empty, "columns": columns}


def product_view(product, prose, dataset, language, image):
    from .live_outputs import _bulk_order_notice, _prose, display_market_checks
    words = TEXT[language]
    score = score_view(product)
    context = market_context(product) if score else None
    view = {"rank": product["rank"], "concept_id": product["concept_id"], "anchor": "product-" + product["concept_id"],
            "title": product["title"], "category": product["category"], "status": product["status"],
            "image": image, "similar_photo": bool(image) and product.get("image_provenance", {}).get("relationship") == "relevant_family",
            "score": score, "compliance": compliance_classes(product), "fit": _fit(product, prose, dataset.get("kpi_total", 13)),
            "prose": {"reason": _prose(prose["reason"], product, dataset, language),
                      "points": [_prose(point, product, dataset, language) for point in prose["points"]],
                      "limitation": _prose(prose.get("limitation", ""), product, dataset, language),
                      "update": _prose(prose.get("update_sentence", ""), product, dataset, language)},
            "market_checks": display_market_checks(product) if not score else [],
            "bulk": next((text for price in product["prices"] if (text := _bulk_order_notice(price, language))), None),
            "prices": _price_rows(product, language) if product["prices"] else None,
            "examples": _examples(product, language) if "example_products" in product and not product["prices"] else None,
            "suppliers": _suppliers(product, language)}
    if score:
        from .opportunity_score import crowding_text, score_text
        view.update(score_line=score_text(product, language), crowd_line=crowding_text(product, language))
        view.update(meta=meta_view(score, context), trends=trends_view(score, context),
                    platform=platform_view(score, context),
                    qc=qc_view(score, context, product.get("quick_commerce_checks")))
    seen, sources = set(), []
    for ref in product["source_refs"]:
        # The same page seen on the same day is one dated link; other dates stay separate.
        if ref.get("url") and (ref["url"], ref["observed_at"][:10]) not in seen:
            seen.add((ref["url"], ref["observed_at"][:10]))
            sources.append((pages.source_name(ref["source"]), ref["url"], short_date(ref["observed_at"])))
    view["sources"] = sources
    return view


def render_document(issue, dataset, narrative, language, *, logo, images):
    from .opportunity_score import load_settings
    words = TEXT[language]
    views = [product_view(product, prose, dataset, language, image)
             for product, prose, image in zip(dataset["products"], narrative["products"], images)]
    days = sorted({ref["observed_at"][:10] for product in dataset["products"] for ref in product["source_refs"]
                   if isinstance(ref.get("observed_at"), str)})
    sites = sorted({pages.source_name(ref["source"]) for product in dataset["products"] for ref in product["source_refs"]
                    if ref.get("url")}, key=str.casefold)
    marker = words["synthetic"] if dataset["evidence_type"] == "synthetic" else None
    footer = words["foot"].format(date=long_date(dataset["release_date"]))
    ctx = {"language": language, "words": words, "views": views, "counts": issue["counts"], "logo": logo,
           "title": narrative["title"], "marker": marker, "footer": footer, "date_long": long_date(dataset["release_date"]),
           "issue_line": pages.weekday_line(language, dataset["release_date"]), "maxima": load_settings()["parts"],
           "sites": sites, "window": date_window(days)}
    sheets = [pages.cover(ctx), pages.glance(ctx), pages.key_page(ctx)]
    for view in views:
        sheets += [pages.product_one(ctx, view), pages.product_two(ctx, view)]
    sheets.append(pages.closing(ctx))
    css = stylesheet(footer=footer, page_word=words["page"], of_word=words["of"], marker=marker)
    return ('<!doctype html><html lang="' + ("en" if language == "en" else "hi-Latn") + '"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><title>' + escape(narrative["title"])
            + '</title><style>' + css + '</style></head><body>' + "".join(sheets) + '</body></html>')


def examples_html(product, language):
    """The example-listing block of one product's market page (also used by tests)."""
    return pages._prices_section({"bulk": None, "prices": None, "examples": _examples(product, language)}, TEXT[language])
