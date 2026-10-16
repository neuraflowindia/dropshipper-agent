"""The report's pages: cover, at a glance, how to read, two pages per product, closing.

Every function returns HTML for one printed sheet from checked, code-owned
facts plus the accepted narrative; nothing here decides a fact. Each sheet
forces a page break after itself, so every product's two pages start fresh.
Ported from the approved mock-up (report redesign, 16 October 2026).
"""

from decimal import Decimal
from html import escape
import re
from urllib.parse import urlsplit

from . import report_charts as charts
from .report_copy import (FIXED_SUPPLIER_REASONS, HI_STATES, PLATFORM_NAMES, QC_NAMES, TEXT, WEEKDAYS, long_date, month_year,
                          short_date)
from .report_visuals import compliance_text

COVER_PRODUCTS = 6
DENSE_GLANCE_ROWS = 12
SHOWN_EXAMPLES = 6
SOURCE_NAMES = {"meta": "Facebook Ads Library", "google-trends": "Google Trends India", "amazon": "Amazon India",
                "flipkart": "Flipkart", "blinkit": "Blinkit", "zepto": "Zepto", "instamart": "Instamart",
                "deodap": "DeoDap", "baapstore": "BaapStore", "dropdash": "Dropdash", "indiamart": "IndiaMART",
                "tradeindia": "TradeIndia"}
TERM_COLUMNS = ("dispatch", "cod", "plain", "label", "fees")
LEGACY_TERM_COLUMNS = ("dispatch", "fees")  # the first supplier-terms display contract shows only these


def esc(value):
    return escape(str(value), quote=True)


def source_name(value):
    return SOURCE_NAMES.get(value, value)


def rupees(amount):
    """'1299.50' -> '₹1,299.50' (Indian digit grouping)."""
    number = Decimal(amount)
    whole = int(number)
    text = "₹" + charts.fmt_in(whole)
    fraction = number - whole
    if fraction:
        text += "." + format(fraction.normalize(), "f").split(".")[1]
    return text


def host(url):
    if not isinstance(url, str):
        return None
    name = urlsplit(url).hostname or ""
    return name[4:] if name.startswith("www.") else name or None


def chip(label, words, *, small=False):
    return (f'<span class="chip chip--{label}{" chip--sm" if small else ""}">{charts.glyph_label(label, 2.8 if small else 3.0)}'
            f'<span>{esc(words["labels"][label])}</span></span>')


def outline_chip(value, cls=""):
    return f'<span class="chip chip--outline {cls}">{esc(value)}</span>'


def compliance_chip(words):
    return f'<span class="chip chip--comply">{charts.icon("shield", 3.2, 2)}<span>{esc(words["compliance_short"])}</span></span>'


def screen_foot(ctx):
    return f'<footer class="screen-foot"><span>{esc(ctx["footer"])}</span><span>{esc(ctx["marker"] or "")}</span></footer>'


def plate(view, words, *, big=True):
    if view["image"]:
        note = (f'<figcaption class="plate-note">{esc(words["photo_note"])}</figcaption>'
                if big and view["similar_photo"] else "")
        return (f'<figure class="plate{" plate--big" if big else ""}"><img src="{view["image"]}" alt="{esc(view["title"])}">'
                f'{note}</figure>')
    sub = f'<span>{esc(words["no_photo_sub"])}</span>' if big else ""
    return (f'<figure class="plate plate--empty{" plate--big" if big else ""}">{charts.icon(view["category"], 15 if big else 11, 1.1)}'
            f'<figcaption><b>{esc(words["no_photo"])}</b>{sub}</figcaption></figure>')


def kpi_band(values, cls="kpis"):
    return (f'<div class="{cls}">' + "".join(f'<div class="kpi"><b>{number}</b><span>{esc(label)}</span></div>'
                                             for number, label in values) + '</div>')


# Cover -------------------------------------------------------------------------

def cover(ctx):
    words, counts, views = ctx["words"], ctx["counts"], ctx["views"]
    scored = all(view["score"] for view in views)
    fourth = ((sum(view["score"]["label"] == "sweet_spot" for view in views), words["kpi"]["sweet"]) if scored else
              (sum(bool(view["compliance"]) for view in views), words["kpi"]["compliance"]))
    kpis = kpi_band([(counts["products"], words["kpi"]["products"]), (counts["new"], words["kpi"]["new"]),
                     (counts["updates"], words["kpi"]["updated"]), fourth], "cover-kpis")
    tiles = ""
    for view in views[:COVER_PRODUCTS]:
        score = (f'<span class="mosaic-score"><b>{view["score"]["score"]}</b>{chip(view["score"]["label"], words, small=True)}</span>'
                 if view["score"] else "")
        tiles += (f'<a class="mosaic-tile" href="#{view["anchor"]}">{plate(view, words, big=False)}'
                  f'<div class="mosaic-cap"><span class="mosaic-rank">{view["rank"]:02d}</span>'
                  f'<span class="mosaic-title">{esc(view["title"])}</span>{score}</div></a>')
    dek = words["count_dek" if scored else "count_dek_unscored"].format(n=counts["products"], new=counts["new"], upd=counts["updates"])
    if ctx["window"]:
        dek += " " + words["dek_window"].format(window=ctx["window"])
    logo = (f'<img class="logo" src="{ctx["logo"]}" alt="NeuraFlow">' if ctx["logo"] else '<p class="logo-text">NeuraFlow</p>')
    marker = f'<p class="cover-marker">{esc(ctx["marker"])}</p>' if ctx["marker"] else ""
    return (f'<section class="sheet cover" id="cover"><div class="cover-inner">'
            f'<div class="cover-top">{logo}<p class="cover-issue">{esc(ctx["issue_line"])}</p></div>'
            f'<div class="cover-title"><p class="kicker">{esc(words["brand_line"])}</p><h1 class="h-display">{esc(ctx["title"])}</h1>'
            f'<p class="cover-dek">{esc(dek)}</p></div>{kpis}<div class="mosaic">{tiles}</div></div>'
            f'<div class="cover-foot"><p>{esc(words["cover_notice"])}</p>{marker}</div></section>')


# At a glance ------------------------------------------------------------------------

def _mixes(ctx):
    words, views = ctx["words"], ctx["views"]
    blocks = []
    if all(view["score"] for view in views):
        labels = charts.LABELS
        label_counts = [sum(view["score"]["label"] == label for view in views) for label in labels]
        legend = "".join(f'<li>{charts.glyph_label(label, 2.8)}<span>{esc(words["labels"][label])}</span><b>{n}</b></li>'
                         for label, n in zip(labels, label_counts))
        blocks.append(f'<div class="mix"><p class="mix-title">{esc(words["mix"]["labels"])}</p>'
                      f'{charts.stacked_bar(label_counts, [charts.LABEL[label] for label in labels])}'
                      f'<ul class="mix-legend">{legend}</ul></div>')
        crowd_counts = [sum(view["score"]["level"] == level for view in views) for level in charts.LEVELS]
        legend = "".join(f'<li><i style="background:{charts.CROWD[level]}"></i><span>{esc(words["crowd"][level])}</span><b>{n}</b></li>'
                         for level, n in zip(charts.LEVELS, crowd_counts))
        blocks.append(f'<div class="mix"><p class="mix-title">{esc(words["mix"]["crowd"])}</p>'
                      f'{charts.stacked_bar(crowd_counts, [charts.CROWD[level] for level in charts.LEVELS])}'
                      f'<ul class="mix-legend">{legend}</ul></div>')
    categories = {}
    for view in views:
        categories[view["category"]] = categories.get(view["category"], 0) + 1
    top = max(categories.values())
    rows = "".join(f'<li><span>{esc(name)}</span>{charts.category_bar(n, top)}<b>{n}</b></li>'
                   for name, n in sorted(categories.items(), key=lambda item: (-item[1], item[0])))
    blocks.append(f'<div class="mix"><p class="mix-title">{esc(words["mix"]["cats"])}</p><ul class="cat-list">{rows}</ul></div>')
    return '<div class="mixes">' + "".join(blocks) + '</div>'


def _title_with_shield(view, words):
    shield = (f'&nbsp;<span class="shield" title="{esc(words["compliance_short"])}">{charts.icon("shield", 3.0, 2)}'
              f'<span class="sr">{esc(words["compliance_short"])}</span></span>') if view["compliance"] else ""
    head, _, last = view["title"].rpartition(" ")
    glued = f'<span class="nw">{esc(last)}{shield}</span>'
    return (esc(head) + " " + glued) if head else glued


def glance(ctx):
    words, counts, views = ctx["words"], ctx["counts"], ctx["views"]
    scored = all(view["score"] for view in views)
    kpis = kpi_band([(counts["products"], words["kpi"]["products"]), (counts["new"], words["kpi"]["new"]),
                     (counts["updates"], words["kpi"]["updated"]),
                     (sum(bool(view["compliance"]) for view in views), words["kpi"]["compliance"])])
    head = "".join(f'<th scope="col" class="c{i}">{esc(h)}</th>' for i, h in enumerate(words["table"]))
    body, crowded = "", False
    for view in views:
        score = view["score"]
        if score and score["level"] == "high" and not crowded:
            crowded = True
            body += f'<tr class="group"><td colspan="6">{esc(words["group_high"])}</td></tr>'
        title = f'<a href="#{view["anchor"]}">{_title_with_shield(view, words)}</a>'
        if score:
            cells = (f'<td class="c2"><span class="scorecell">{charts.compartment_bar(score["points"], score["maxima"], label=words["score"] + " " + str(score["score"]))}'
                     f'<b>{score["score"]}</b></span></td><td class="c3">{chip(score["label"], words, small=True)}</td>'
                     f'<td class="c4"><span class="crowdcell">{charts.crowd_pips(score["level"])}<span>{esc(words["crowd"][score["level"]])}</span></span></td>')
        else:
            cells = (f'<td class="c2"><span class="soft">{esc(words["not_scored"])}</span></td><td class="c3"></td><td class="c4"></td>')
        body += (f'<tr><td class="c0">{view["rank"]:02d}</td><td class="c1"><span class="pt">{title}</span>'
                 f'<span class="meta">{esc(view["category"])} · {esc(words["status"][view["status"]])}</span></td>'
                 f'{cells}<td class="c5">{view["fit"]["matches"]}<span class="of">/{view["fit"]["total"]}</span></td></tr>')
    dense = len(views) > DENSE_GLANCE_ROWS
    dek = (words["glance_dek_short"] if dense else words["glance_dek"].format(n=counts["products"])) if scored else \
        words["glance_dek_unscored"].format(n=counts["products"])
    key = (f'<p class="table-key">{esc(words["score_key"])} {charts.icon("shield", 3.0, 2)} = {esc(words["compliance_short"])}.</p>'
           if scored else f'<p class="table-key">{charts.icon("shield", 3.0, 2)} = {esc(words["compliance_short"])}.</p>')
    return (f'<section class="sheet glance{" glance--dense" if dense else ""}" id="glance"><header class="page-head">'
            f'<p class="kicker">{esc(words["glance_kicker"])} · {esc(ctx["date_long"])}</p><h1 class="h-title">{esc(words["glance_title"])}</h1>'
            f'<p class="dek">{esc(dek)}</p></header>{kpis}{_mixes(ctx)}'
            f'<table class="league"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{key}{screen_foot(ctx)}</section>')


# How to read ---------------------------------------------------------------------------

def key_page(ctx):
    words = ctx["words"]
    maxima = ctx["maxima"]
    demo = {"want": 14, "early": 17, "room": 12, "product": 19}
    if any(demo[part] > maxima[part] for part in charts.PARTS):
        demo = {part: maxima[part] // 2 for part in charts.PARTS}
    labels = "".join(f'<li>{chip(label, words)}<span>{esc(words["label_meaning"][label])}</span></li>' for label in charts.LABELS)
    parts = "".join(f'<li><p><b>{esc(words["parts"][part])}</b> <span class="max">{esc(words["up_to"].format(n=maxima[part]))}</span></p>'
                    f'<p>{esc(words["key_parts"][part])}</p></li>' for part in charts.PARTS)
    states = "".join(f'<li>{charts.glyph_state(state, 3.6)}<span><b>{esc(words["states"][state])}.</b> {esc(words["key_states"][state])}</span></li>'
                     for state in ("Meets", "Does not meet", "Unclear"))
    signals = [(f'<i class="dot dot--navy"></i><i class="dot dot--maroon"></i>', words["key_meta"]),
               ('<i class="band band--sand"></i><i class="band band--rose"></i>', words["key_trends"]),
               (outline_chip(words["roles"]["bestseller"], "chip--role"), words["key_plat"]),
               (charts.glyph_state("in_stock", 3.4) + charts.glyph_state("not_in_stock", 3.4) + charts.glyph_state("not_checked", 3.4),
                words["key_qc"])]
    signal_items = "".join(f'<li><span class="sig">{symbol}</span><span><b>{esc(title)}</b> {esc(text)}</span></li>'
                           for symbol, (title, text) in signals)
    supplier = esc(words["key_sup_text"]).replace("{yes}", charts.glyph_state("yes", 3.2)).replace(
        "{no}", charts.glyph_state("no", 3.2)).replace("{unknown}", charts.glyph_state("not_stated", 3.2))
    total = sum(demo.values())
    return (f'<section class="sheet key" id="key"><header class="page-head"><p class="kicker">{esc(words["key_kicker"])}</p>'
            f'<h1 class="h-title">{esc(words["key_title"])}</h1><p class="dek">{esc(words["key_dek"])}</p></header>'
            f'<section class="key-block key-score-block"><h2 class="h-module">{esc(words["key_score_title"])}</h2>'
            f'<div class="key-score">{charts.score_ring(demo, maxima, total, caption=words["out_of"], label=words["score"] + " " + str(total))}'
            f'<p>{esc(words["key_score_text"])}</p></div><ul class="key-parts">{parts}</ul></section>'
            f'<div class="key-grid"><div class="key-col">'
            f'<section class="key-block"><h2 class="h-module">{esc(words["key_label_title"])}</h2><ul class="key-labels">{labels}</ul></section>'
            f'<section class="key-block"><h2 class="h-module">{esc(words["key_crowd_title"])}</h2><div class="key-crowd">'
            f'{charts.crowd_gauge("medium", width=26, low=words["crowd"]["low"], high=words["crowd"]["high"])}<p>{esc(words["key_crowd_text"])}</p></div></section>'
            f'<section class="key-block"><h2 class="h-module">{esc(words["key_comply_title"])}</h2><div class="key-comply">'
            f'{compliance_chip(words)}<p>{esc(words["key_comply_text"])}</p></div></section>'
            f'<section class="key-block"><h2 class="h-module">{esc(words["key_sup_title"])}</h2><p class="key-p">{supplier}</p></section></div>'
            f'<div class="key-col"><section class="key-block"><h2 class="h-module">{esc(words["key_check_title"])}</h2><ul class="key-states">{states}'
            f'<li><span class="est">{esc(words["estimated"])}</span><span>{esc(words["key_estimated"])}</span></li></ul>'
            f'<div class="key-pips">{charts.fit_pips(["Meets"] * 7 + ["Unclear", "Does not meet", "Unclear"])}<span>{esc(words["key_pips"])}</span></div></section>'
            f'<section class="key-block"><h2 class="h-module">{esc(words["key_signals_title"])}</h2><ul class="key-signals">{signal_items}</ul></section></div></div>'
            f'<p class="key-not-line">{esc(words["key_not_line"])}</p>{screen_foot(ctx)}</section>')


# Product page one --------------------------------------------------------------------------

def _part_items(view, part, words):
    score = view["score"]
    if part == "room":
        return f'{esc(words["items"]["crowding"])}: <b>{esc(words["crowd"][score["level"]])}</b>'
    if part == "product":
        states = view["fit"]["states"]
        return " · ".join(f'{esc(words["states"][state])}&nbsp;<b>{states.count(state)}</b>'
                          for state in ("Meets", "Unclear", "Does not meet") if states.count(state))
    return " · ".join(f'{esc(words["items"][name])}&nbsp;<b>{points}</b>' for name, points in score["items"][part].items())


def _adds_up(view, words):
    score = view["score"]
    columns = "".join(
        f'<li class="part"><p class="part-head"><b>{esc(words["parts"][part])}</b><span class="part-val"><b>{score["points"][part]}</b>/{score["maxima"][part]}</span></p>'
        f'{charts.part_meter(score["points"][part], score["maxima"][part])}<p class="part-items">{_part_items(view, part, words)}</p></li>'
        for part in charts.PARTS)
    return f'<section class="adds-up"><h2 class="h-module">{esc(words["adds_up"])}</h2><ul class="parts">{columns}</ul></section>'


def _fit_section(view, words):
    fit = view["fit"]
    if not fit["rows"]:
        return ""
    items = ""
    for row in fit["rows"]:
        state = row["state"]
        state_cls = "crit-state sr" if state == "Meets" else "crit-state"
        estimated = f' <span class="est">{esc(words["estimated"])}</span>' if row["estimated"] else ""
        items += (f'<li>{charts.glyph_state(state, 3.8)}<div><p class="crit"><b>{esc(row["name"])}</b>'
                  f'<span class="{state_cls}">{esc(words["states"][state])}</span>{estimated}</p>'
                  f'<p class="crit-why">{esc(row["phrase"])}</p></div></li>')
    tier = f'<b>{esc(words["tier"][fit["tier"]])}</b> · ' if fit["tier"] else ""
    return (f'<section class="fit"><div class="fit-head"><h2 class="h-section">{esc(words["fit"])}</h2>'
            f'<p class="fit-sum">{charts.fit_pips(fit["states"])}<span>{tier}{esc(words["fit_count"].format(m=fit["matches"], t=fit["total"]))}</span></p></div>'
            f'<ul class="fit-list">{items}</ul></section>')


def product_one(ctx, view):
    words = ctx["words"]
    score = view["score"]
    chips = ""
    if score:
        chips += chip(score["label"], words)
    if view["fit"]["tier"]:
        chips += outline_chip(words["tier"][view["fit"]["tier"]], "chip--fit")
    label_line = (f'<p class="label-line">{charts.glyph_label(score["label"], 3.0)}<span>{esc(words["label_meaning"][score["label"]])}</span></p>'
                  if score else "")
    changed = (f'<p class="changed"><b>{esc(words["changed"])}:</b> {esc(view["prose"]["update"])}</p>'
               if view["status"] == "Updated" and view["prose"]["update"] else "")
    if score:
        dial = (f'<div class="pa-dial">{charts.score_ring(score["points"], score["maxima"], score["score"], caption=words["out_of"], label=view["score_line"])}'
                f'<p>{esc(words["score"])}</p></div>')
    else:
        dial = (f'<div class="pa-dial pa-dial--plain"><p class="stat">{view["fit"]["matches"]}/{view["fit"]["total"]}</p>'
                f'<p>{esc(words["not_scored"])}</p></div>')
    banner = (f'<div class="comply">{charts.icon("shield", 4.4, 1.8)}<p><b>{esc(words["compliance_title"])}.</b> '
              f'{esc(compliance_text(view["compliance"], ctx["language"]))}</p></div>') if view["compliance"] else ""
    points = "".join(f'<li>{esc(point)}</li>' for point in view["prose"]["points"])
    points = f'<ol class="points">{points}</ol>' if points else ""
    note = (f'<div class="note"><p class="note-label">{esc(words["keep"])}</p><p class="note-text">{esc(view["prose"]["limitation"])}</p></div>'
            if view["prose"]["limitation"] else "")
    kicker = f'{esc(view["category"])} · {esc(words["status_long"][view["status"]])}'
    return (f'<section class="sheet product-a" id="{view["anchor"]}" data-concept-id="{esc(view["concept_id"])}">'
            f'<header class="pa-head"><div class="pa-photo">{plate(view, words)}<span class="rank-badge">{view["rank"]:02d}</span></div>'
            f'<div class="pa-titles"><p class="kicker">{kicker}</p><h1 class="h-title">{esc(view["title"])}</h1>'
            f'<div class="chips">{chips}</div>{label_line}{changed}</div>{dial}</header>{banner}'
            f'{_adds_up(view, words) if score else ""}'
            f'<section class="why"><h2 class="h-section">{esc(words["why"])}</h2><p class="lead">{esc(view["prose"]["reason"])}</p>{points}{note}</section>'
            f'{_fit_section(view, words)}{screen_foot(ctx)}</section>')


# Product page two ---------------------------------------------------------------------------

def _tile(title, source, body, cls):
    source_line = f'<p class="tile-src">{esc(source)}</p>' if source else ""
    return f'<article class="tile {cls}"><header><p class="tile-title">{esc(title)}</p>{source_line}</header>{body}</article>'


def _dated(name, day):
    return name + (" · " + short_date(day.isoformat()) if day else "")


def _crowd_tile(view, words):
    score = view["score"]
    facts = score["facts"]["crowding"]
    counts = facts["counts"] or {}
    listings = counts.get("relevant_listings") or {}
    branded, hidden = counts.get("listings_with_brand") or 0, counts.get("brand_not_shown") or 0
    brands = (esc(words["c_brands_hidden"]) if not branded else str(counts.get("brands_or_sellers", 0))
              + (f' <span class="soft">({esc(words["c_nobrand"].format(n=hidden))})</span>' if hidden else ""))
    ratings = counts.get("max_ratings")
    ratings = charts.fmt_in(ratings) if type(ratings) is int else esc(words["c_not_shown"])
    facts_html = (f'<dl class="facts"><div><dt>{esc(words["c_listings"])}</dt><dd>Amazon {listings.get("amazon", 0)} · Flipkart {listings.get("flipkart", 0)}</dd></div>'
                  f'<div><dt>{esc(words["c_brands"])}</dt><dd>{brands}</dd></div>'
                  f'<div><dt>{esc(words["c_ratings"])}</dt><dd>{ratings}</dd></div>'
                  f'<div><dt>{esc(words["c_best"])}</dt><dd>{counts.get("bestseller_hits", 0)}</dd></div></dl>')
    level = score["level"]
    body = (f'<div class="crowd-body"><div class="crowd-left">{charts.crowd_gauge(level, low=words["crowd"]["low"], high=words["crowd"]["high"], label=view["crowd_line"])}'
            f'<p class="stat">{esc(words["crowd"][level])}</p><p class="soft">{esc(words["c_room"].format(p=score["points"]["room"], m=score["maxima"]["room"]))}</p></div>{facts_html}</div>')
    return _tile(words["t_crowd"], words["t_crowd_src"], body, "tile--crowd")


def _meta_tile(view, words, ctx):
    meta = view["meta"]
    advertisers = meta["advertisers"] or 0
    noun = words["m_adv_one"] if advertisers == 1 else words["m_adv"]
    ads = (words["m_ads"].format(r=meta["relevant"], a=meta["active"]) if meta["active"] is not None
           else words["m_ads_only"].format(r=meta["relevant"]))
    body = (f'<p class="stat-line"><span class="stat">{advertisers}</span> <span>{esc(noun)}</span>'
            f'<span class="soft">{esc(ads)}</span></p>')
    notes = []
    if not meta["relevant"]:
        notes.append(words["m_none"])
    if meta["starts"] is not None and meta["end"] is not None:
        body += charts.meta_timeline(meta["starts"], meta["end"], label=f'{len(meta["starts"])} {words["m_adv"]}')
        body += (f'<p class="legend"><span><i class="dot dot--navy"></i>{esc(words["m_older"])}</span>'
                 f'<span><i class="dot dot--maroon"></i>{esc(words["m_recent"])}</span></p>')
        if meta["undated"]:
            notes.append(words["m_undated"].format(n=meta["undated"]))
        elif meta["unlinked"]:
            notes.append(words["m_unlinked"])
    elif meta["recent"]:
        notes.append(words["m_recent_count"].format(n=meta["recent"]))
    body += "".join(f'<p class="tile-note">{esc(note)}</p>' for note in notes)
    return _tile(words["t_meta"], _dated(words["t_meta_src"], meta["observed"]), body, "tile--meta")


def _trends_tile(view, words):
    trends = view["trends"]
    state = trends["state"]
    cls = {"rising": "up", "rising_strongly": "up", "steady": "flat", "none": "none"}[state]
    body = f'<p class="trend-state trend-state--{cls}">{esc(words["tr_state"][state])}</p>'
    if trends["points"] and len(trends["points"]) >= 2:
        first, last = trends["points"][0]["start"], trends["points"][-1]["start"]
        body += charts.trends_spark(trends["points"], trends["windows"], left=month_year(first),
                                    right=month_year(last), label=words["t_trends"])
        if trends["recent"] is not None:
            body += f'<p class="tile-note">{esc(words["tr_cap"].format(r=trends["recent"], p=trends["previous"]))}</p>'
        else:
            body += f'<p class="tile-note">{esc(words["tr_cap_short"])}</p>'
    else:
        body += charts.empty_spark(message=words["tr_no_chart"] if state != "none" else "")
    return _tile(words["t_trends"], _dated(words["t_trends_src"], trends["collected"]), body, "tile--trends")


def _platform_tile(view, words):
    platform = view["platform"]
    rows = ""
    for row in platform["rows"]:
        state = row["state"]
        if state == "unknown":
            symbol, word = "not_checked", words["p_state"]["not_checked"]
        else:
            symbol = {"listed": "listed", "not_found": "not_found", "delisted": "not_found",
                      "pending": "not_checked", "not_checked": "not_checked"}[state]
            word = words["p_state"][state]
        roles = "".join(outline_chip(words["roles"].get(role, role), "chip--role") for role in row["roles"])
        price = (f'<span class="p-price">{esc(rupees(row["price"]))}</span>' if row["price"] else
                 '<span class="p-price soft">–</span>')
        rows += (f'<li><span class="p-name">{esc(PLATFORM_NAMES[row["route"]])}</span><span class="p-state">'
                 f'{charts.glyph_state(symbol, 3.0)}{esc(word)}</span>{price}<span class="p-roles">{roles}</span></li>')
    body = f'<ul class="plat">{rows}</ul><p class="tile-note">₹ = {esc(words["p_price"])}</p>'
    if platform["stale"]:
        body += f'<p class="tile-note">{esc(words["p_stale"])}</p>'
    return _tile(words["t_plat"], _dated(words["t_plat_src"], platform["checked"]), body, "tile--plat")


def _qc_tile(view, words):
    qc = view["qc"]
    labels = words["qc"]
    if qc["rows"] is None:
        body = (f'<p class="tile-note"><b>{esc(words["qc_in"].format(places=", ".join(qc["in_stock"])))}</b></p>'
                if qc["in_stock"] else f'<p class="tile-note">{esc(words["qc_basis_none"])}</p>')
        return _tile(words["t_qc"], words["t_qc_src"], body, "tile--qc")
    head = "".join(f'<th scope="col">{esc(place)}</th>' for place in qc["places"])
    rows = ""
    for app, cells in qc["rows"].items():
        rows += (f'<tr><th scope="row">{esc(QC_NAMES[app])}</th>' + "".join(
            f'<td>{charts.glyph_state(cell, 3.2)}<span class="sr">{esc(labels[cell])}</span></td>' for cell in cells) + '</tr>')
    summary = (words["qc_sum" if len(qc["checked"]) != 1 else "qc_sum_one"].format(k=len(qc["in_stock"]), n=len(qc["checked"]))
               if qc["checked"] else words["qc_none"])
    body = (f'<table class="qc"><thead><tr><th></th>{head}</tr></thead><tbody>{rows}</tbody></table>'
            f'<p class="tile-note"><b>{esc(summary)}</b></p><p class="legend legend--qc">'
            + "".join(f'<span>{charts.glyph_state(state, 2.6)}{esc(labels[state])}</span>' for state in ("in_stock", "not_in_stock", "not_checked"))
            + '</p>')
    return _tile(words["t_qc"], _dated(words["t_qc_src"], qc["observed"]), body, "tile--qc")


def _checks_tile(view, words, language):
    """Packets without the opportunity score keep their saved market checks."""
    def state(value):
        return HI_STATES.get(value, value) if language == "hi-Latn" else value
    rows = "".join(f'<div><dt>{esc(check["source"])}</dt><dd>{esc(state(check["state"]))}. <span class="soft">{esc(check["detail"])}</span></dd></div>'
                   for check in view["market_checks"])
    return f'<div class="tiles"><article class="tile"><dl class="facts">{rows}</dl></article></div>' if rows else ""


def _price_rows(rows, words):
    if not rows:
        return f'<p class="price-empty">{esc(words["price_none"])}</p>'
    items = ""
    for figure, what, source, url in rows:
        figure_cls = ("price" if figure.startswith("₹") and len(figure) <= 12 else
                      "price price--text price--none" if figure == words["no_quote"] else "price price--text")
        what = f'<a href="{esc(url)}">{esc(what)}</a>' if url else esc(what)
        items += (f'<li><b class="{figure_cls}">{esc(figure)}</b><span class="p-what">{what}</span>'
                  f'<span class="soft">{esc(source)}</span></li>')
    return f'<ul class="price-list">{items}</ul>'


def _prices_section(view, words):
    bulk = f'<p class="bulk">{esc(view["bulk"])}</p>' if view["bulk"] else ""
    if view["prices"] is not None:
        retail, supplier = view["prices"]
        return (f'<section class="prices-sec"><h2 class="h-module">{esc(words["prices"])}</h2><div class="prices">'
                f'<div><p class="sub">{esc(words["retail"])}</p>{_price_rows(retail, words)}</div>'
                f'<div><p class="sub">{esc(words["supplier"])}</p>{_price_rows(supplier, words)}</div></div>{bulk}</section>')
    examples = view["examples"]
    if examples is None:
        return f'<section class="prices-sec"><h2 class="h-module">{esc(words["prices"])}</h2>{_price_rows([], words)}{bulk}</section>'
    if not examples["rows"]:
        message = words["ex_omitted"] if examples["omitted_for_size"] else words["ex_none"]
        return f'<section class="ex-sec"><h2 class="h-module">{esc(words["examples"])}</h2><p class="price-empty">{esc(message)}</p>{bulk}</section>'
    columns = []
    for key, rows in (("retail_examples", examples["shops"]), ("supplier_examples", examples["leads"])):
        if rows:
            columns.append(f'<div><p class="sub">{esc(words[key])}</p>{_price_rows(rows, words)}</div>')
    more = f'<p class="tile-note">{esc(words["ex_more"].format(n=examples["more"]))}</p>' if examples["more"] else ""
    more += "".join(f'<p class="tile-note">{esc(note)}</p>' for note in examples.get("notes", []))
    return (f'<section class="ex-sec"><h2 class="h-module">{esc(words["price_examples"])}</h2><div class="prices">{"".join(columns)}</div>'
            f'<p class="tile-note">{esc(words["ex_note"])}</p>{more}{bulk}</section>')


def _term_cell(value, words, column):
    symbol = {"yes": "yes", "no": "no", "optional": "Unclear", "conflicting": "conflicting", "not_stated": "not_stated"}[value]
    return (f'<td data-term="{esc(words["terms"][column])}"><span class="term">{charts.glyph_state(symbol, 3.2)}'
            f'{esc(words["tv"][value])}</span></td>')


def _scoped_block(row, words):
    """A lead whose terms differ by offer: full-width paragraphs (one per company or offer), so a long
    lead may continue on the next page between paragraphs, never inside one."""
    name = f'<a href="{esc(row["url"])}">{esc(row["name"])}</a>' if row["url"] else esc(row["name"])
    details = " · ".join(esc(part) for part in row["details"] if part)
    lines = "".join(f'<li>{" · ".join(esc(line) for line in group)}</li>' for group in scoped_groups(row["scoped"]))
    links = " · ".join(f'<a href="{esc(url)}">{esc(label)}</a>' for label, url in row["links"])
    links = f'<p class="s-links">{links}</p>' if links else ""
    head = (f'<p class="s-head"><b>{name}</b>{f" <span class=\"soft\">· {details}</span>" if details else ""}'
            f' · <b>{esc(words["scoped"])}</b></p>')
    return f'<div class="sup-block s-scoped">{head}<ul>{lines}</ul>{links}</div>'


def _suppliers_section(view, words):
    suppliers = view["suppliers"]
    if not suppliers["rows"]:
        return (f'<section class="sup-sec"><h2 class="h-module">{esc(words["suppliers"])}</h2>'
                f'<p class="sup-empty">{esc(suppliers["empty"])}</p></section>')
    columns = suppliers["columns"]
    span = len(columns) + 2
    table_rows = [row for row in suppliers["rows"] if row["scoped"] is None]
    blocks = "".join(f'<tr><td>{_scoped_block(row, words)}</td></tr>' for row in suppliers["rows"] if row["scoped"] is not None)
    if blocks:
        # A one-column table: Chrome repeats its head on every page it reaches, so a long lead
        # continued on the next page still names its product.
        context = f'{view["rank"]:02d} · {view["title"]} · {words["suppliers"]}'
        blocks = f'<table class="sup-cont"><thead><tr><th scope="col">{esc(context)}</th></tr></thead><tbody>{blocks}</tbody></table>'

    table = ""
    if table_rows:
        head = (f'<th scope="col" class="s-name">{esc(words["suppliers"])}</th><th scope="col" class="s-moq">{esc(words["moq"])}</th>'
                + "".join(f'<th scope="col">{esc(words["terms"][column])}</th>' for column in columns))
        body = ""
        for row in table_rows:
            name = f'<a href="{esc(row["url"])}">{esc(row["name"])}</a>' if row["url"] else esc(row["name"])
            details = " · ".join(esc(part) for part in row["details"] if part)
            name_cell = f'<th scope="row" class="s-name"><b>{name}</b>{f"<span>{details}</span>" if details else ""}</th>'
            cells = "".join(_term_cell(row["terms"][column], words, column) for column in columns)
            terms = (f'<tr class="sup-terms"><td colspan="{span}">{esc(words["stated_terms"])}: {esc(row["terms_text"])}</td></tr>'
                     if row["terms_text"] else "")
            body += (f'<tbody class="sup-row{" sup-row--terms" if terms else ""}"><tr>{name_cell}<td class="s-moq" data-term="{esc(words["moq"])}">'
                     f'<b>{esc(row["moq"])}</b></td>{cells}</tr>{terms}</tbody>')
        table = f'<table class="sup"><thead><tr>{head}</tr></thead>{body}</table>'
    notes = "".join(f'<p class="sup-note">{esc(note)}</p>' for note in suppliers["notes"])
    return (f'<section class="sup-sec">{table}{blocks}'
            f'{notes}<p class="sup-note">{esc(words["sup_note"])}</p></section>')


_GROUP_HEAD = re.compile(r"(?:Company information|Company ki jankari|Offer \d+): ")


def scoped_groups(lines):
    """The fixed scoped-supplier lines as one paragraph per company or offer (same words, same order)."""
    groups = []
    for line in lines:
        if not groups or _GROUP_HEAD.match(line):
            groups.append([line])
        else:
            groups[-1].append(line)
    return groups


def source_groups(sources):
    """Links seen on the same site on the same day share one label: 'Facebook Ads Library 5 Oct: 1 · 2 · 3'."""
    groups = {}
    for name, url, day in sources:
        groups.setdefault((name, day), []).append(url)
    return [(name, day, urls) for (name, day), urls in groups.items()]


def _sources_section(view, words):
    items = []
    for name, day, urls in source_groups(view["sources"]):
        if len(urls) == 1:
            items.append(f'<li><a href="{esc(urls[0])}">{esc(name)}</a> <span>{esc(day)}</span></li>')
        else:
            numbered = " ".join(f'<a href="{esc(url)}">{number}</a>' for number, url in enumerate(urls, 1))
            items.append(f'<li>{esc(name)} <span>{esc(day)}:</span> {numbered}</li>')
    links = "".join(items)
    body = f'<ul class="sources">{links}</ul>' if links else f'<p class="sup-note">{esc(words["sources_none"])}</p>'
    return f'<section class="src-sec"><h2 class="h-module">{esc(words["sources"])}</h2>{body}</section>'


def product_two(ctx, view):
    words = ctx["words"]
    score = view["score"]
    chip_html = chip(score["label"], words, small=True) if score else ""
    score_html = f'<span class="pb-score"><b>{score["score"]}</b>/100</span>' if score else ""
    if score:
        tiles = (f'<div class="tiles tiles--2">{_crowd_tile(view, words)}{_meta_tile(view, words, ctx)}</div>'
                 f'<div class="tiles tiles--3">{_trends_tile(view, words)}{_platform_tile(view, words)}{_qc_tile(view, words)}</div>')
    else:
        tiles = _checks_tile(view, words, ctx["language"])
    return (f'<section class="sheet product-b" data-concept-id="{esc(view["concept_id"])}"><header class="pb-head"><div class="pb-strip">'
            f'<span class="pb-rank">{view["rank"]:02d}</span><h2 class="pb-title">{esc(view["title"])}<span class="pb-sec">{esc(words["market"])}</span></h2>'
            f'{chip_html}{score_html}</div></header>{tiles}{_prices_section(view, words)}{_suppliers_section(view, words)}'
            f'{_sources_section(view, words)}{screen_foot(ctx)}</section>')


# Closing ------------------------------------------------------------------------------------------

def closing(ctx):
    words, counts = ctx["words"], ctx["counts"]
    steps = "".join(f'<li><span class="step-n">{number}</span><p class="step-t">{esc(title)}</p><p>{esc(text)}</p></li>'
                    for number, (title, text) in enumerate(words["steps"], 1))
    sites = "".join(f'<li>{esc(name)}</li>' for name in ctx["sites"])
    sites = f'<ul class="sites">{sites}</ul>' if sites else f'<p class="sup-note">{esc(words["sites_none"])}</p>'
    facts = words["issue_facts"]
    rows = [(facts["date"], ctx["date_long"])]
    if ctx["window"]:
        rows.append((facts["window"], ctx["window"]))
    rows += [(facts["types"], words["issue_types"].format(n=counts["products"], new=counts["new"], upd=counts["updates"])),
             (facts["languages"], words["issue_languages"]), (facts["workbook"], words["issue_workbook"])]
    issue = "".join(f'<div><dt>{esc(label)}</dt><dd>{esc(value)}</dd></div>' for label, value in rows)
    notes = "".join(f'<li>{esc(note)}</li>' for note in words["notes"])
    synthetic = f'<p class="synthetic-note">{esc(words["synthetic_note"])}</p>' if ctx["marker"] else ""
    return (f'<section class="sheet closing" id="method"><header class="page-head"><p class="kicker">{esc(words["close_kicker"])}</p>'
            f'<h1 class="h-title">{esc(words["close_title"])}</h1><p class="dek">{esc(words["close_dek"])}</p></header>'
            f'<ol class="steps">{steps}</ol><div class="close-grid"><section><h2 class="h-module">{esc(words["sites_title"])}</h2>{sites}</section>'
            f'<section><h2 class="h-module">{esc(words["issue_title"])}</h2><dl class="issue-facts">{issue}</dl></section></div>'
            f'<aside class="notes-box"><h2 class="h-module">{esc(words["notes_title"])}</h2><ul>{notes}</ul></aside>{synthetic}'
            f'{screen_foot(ctx)}</section>')


def weekday_line(language, value):
    from datetime import date
    words = TEXT[language]
    day = date.fromisoformat(value[:10])
    return words["issue_line"].format(weekday=WEEKDAYS[language][day.weekday()], date=long_date(value))


def fixed_reason(reason, language):
    if language == "hi-Latn":
        return FIXED_SUPPLIER_REASONS.get(reason, reason)
    return reason
