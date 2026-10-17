"""Assemble one report edition: product views from checked facts, then the pages.

``live_outputs.render_html`` validates the packet and resolves the owned
images, then calls ``render_document``. Every value shown comes from the
frozen facts, the code-owned score and wording, or the accepted narrative.
"""

from html import escape

from . import report_pages as pages
from .report_copy import SOURCE_LABELS, TEXT, date_window, long_date, medium_date, short_date
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


def _report_money(currency, amount):
    return pages.rupees(amount) if currency == "INR" else currency + " " + amount


def _covered_by(shared, reason, supplier):
    """Whether the shared note printed under the leads already says what this lead's own caveat says:
    the identical sentence, or the plural note for the singular "still needs matching" caveat."""
    from .report_selection import LEGACY_LEAD_LIMITATION, LEGACY_LEADS_LIMITATION
    return reason == shared or (shared == LEGACY_LEADS_LIMITATION and reason == LEGACY_LEAD_LIMITATION
                                and supplier.get("type_scope_limitation") == LEGACY_LEAD_LIMITATION)


def _suppliers(product, language):
    from .fit_policy import FIT_POLICY_ID
    from .live_outputs import (SUPPLIER_TERMS_DISPLAY_VERSION, _supplier_minimum_order, _supplier_terms_version,
                               supplier_terms_display)
    from .report_source_facts import CONTRACTS as SOURCE_ROUTE_CONTRACTS
    from .supplier_scoped_display import is_scoped_supplier, scoped_supplier_groups
    words = TEXT[language]
    every = product.get("source_route_contract") in SOURCE_ROUTE_CONTRACTS
    shown = product["suppliers"] if every else product["suppliers"][:2]
    terms_version = _supplier_terms_version(product)
    shared = product.get("supplier_limitation")
    # The shared note is printed under the leads whenever any lead is shown.
    printed = shared if shown and isinstance(shared, str) and shared.strip() else None
    rows = []
    for supplier in shown:
        seen = supplier.get("source_capture_at") or supplier.get("reviewed_at")
        reason = supplier.get("reason") or ""
        details = [words["lead_seen"].format(d=short_date(seen)) if isinstance(seen, str) else None]
        if reason and not _covered_by(printed, reason, supplier):
            details.append(pages.fixed_reason(reason, language))
        row = {"name": supplier["name"], "url": supplier.get("url"), "details": details, "scoped": None}
        if is_scoped_supplier(supplier):
            row["scoped"] = scoped_supplier_groups(supplier, language, day=medium_date, money=_report_money)
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
    notes = [pages.fixed_reason(printed, language)] if printed else []
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
    view["sources"] = _sources(product, view, language)
    return view


def site_name(source, language):
    """The client name of a saved source label ("meta" -> "Facebook Ads Library")."""
    label = SOURCE_LABELS.get(source)
    return label[language] if label else pages.source_name(source)


def _sources(product, view, language):
    """Every dated source this product's facts rest on, linked when a page address was saved:
    the frozen source references (with or without a link), plus the dropshipping-platform and
    quick-commerce checks the market tiles read, which keep no page link. The same page seen on
    the same day is one entry; other dates stay separate."""
    from .report_copy import PLATFORM_NAMES, QC_NAMES
    seen, sources = set(), []

    def add(name, url, day):
        if (name, url, day) not in seen:
            seen.add((name, url, day))
            sources.append((name, url, day))

    for ref in product["source_refs"]:
        add(site_name(ref["source"], language), ref.get("url") or None, short_date(ref["observed_at"]))
    platform = view.get("platform")
    if platform and platform["checked"]:
        for row in platform["rows"]:
            if row["state"] not in ("not_checked", "unknown"):
                add(PLATFORM_NAMES[row["route"]], None, short_date(platform["checked"].isoformat()))
    qc = view.get("qc")
    if qc and qc["rows"]:
        for app, day in qc["app_dates"].items():
            add(QC_NAMES[app], None, short_date(day.isoformat()))
    return sources


def render_document(issue, dataset, narrative, language, *, logo, images):
    from .opportunity_score import load_settings
    words = TEXT[language]
    views = [product_view(product, prose, dataset, language, image)
             for product, prose, image in zip(dataset["products"], narrative["products"], images)]
    days = sorted({ref["observed_at"][:10] for product in dataset["products"] for ref in product["source_refs"]
                   if isinstance(ref.get("observed_at"), str)})
    sites = sorted({name for view in views for name, _, _ in view["sources"]}, key=str.casefold)
    marker = words["synthetic"] if dataset["evidence_type"] == "synthetic" else None
    footer = words["foot"].format(date=long_date(dataset["release_date"]))
    settings = load_settings()
    ctx = {"language": language, "words": words, "views": views, "counts": issue["counts"], "logo": logo,
           "title": narrative["title"], "marker": marker, "footer": footer, "date_long": long_date(dataset["release_date"]),
           "issue_line": pages.weekday_line(language, dataset["release_date"]), "maxima": settings["parts"],
           "settings": settings, "sites": sites, "window": date_window(days)}
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
