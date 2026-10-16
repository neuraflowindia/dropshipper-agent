#!/usr/bin/env python3
"""Build the report redesign mock-ups (sample data) as self-contained HTML.

Usage:  python build_mockup.py            -> writes ../mockup-en.html and ../mockup-hi-Latn.html

No network, no script tags: fonts and images are embedded as data URLs so the
page prints identically in the production engine (Windows Chrome headless,
scripts/render_live_pdf.py) and on any phone.
"""

from base64 import b64encode
from html import escape
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import components as C  # noqa: E402
from copy_text import COPY  # noqa: E402
from sample_data import ISSUE, MAXIMA, ITEMS, CRITERIA, SPREADS, products  # noqa: E402

ASSETS = HERE / "assets"
OUT = HERE.parent
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


# --- Embedded assets ------------------------------------------------------------------------------

def data_url(path, mime):
    return f"data:{mime};base64," + b64encode(path.read_bytes()).decode("ascii")


FONT_FACES = [("NF Serif", 400, "normal", "nf-serif-text-400"), ("NF Serif", 600, "normal", "nf-serif-text-600"),
              ("NF Serif", 400, "italic", "nf-serif-text-italic-400"), ("NF Serif Head", 600, "normal", "nf-serif-head-600"),
              ("NF Sans", 400, "normal", "nf-sans-400"), ("NF Sans", 500, "normal", "nf-sans-500"),
              ("NF Sans", 600, "normal", "nf-sans-600"), ("NF Sans Cond", 500, "normal", "nf-sans-cond-500"),
              ("NF Sans Cond", 600, "normal", "nf-sans-cond-600")]


def font_css():
    return "".join(
        f'@font-face{{font-family:"{fam}";font-weight:{w};font-style:{st};font-display:block;'
        f'src:url({data_url(ASSETS / "fonts" / (name + ".woff2"), "font/woff2")}) format("woff2");}}'
        for fam, w, st, name in FONT_FACES)


IMAGES = {}


def image(key):
    if key not in IMAGES:
        IMAGES[key] = data_url(ASSETS / "images" / f"illustrative-{key}.jpg", "image/jpeg")
    return IMAGES[key]


def long_date(value):
    y, m, d = value.split("-")
    return f"{int(d)} {MONTHS[int(m) - 1]} {y}"


def window():
    a, b = ISSUE["research"]
    return f"{int(a[8:])}–{int(b[8:])} {MONTHS[int(b[5:7]) - 1]} {b[:4]}"


# --- Small HTML parts ----------------------------------------------------------------------------

def chip(label, t, *, small=False):
    return (f'<span class="chip chip--{label}{" chip--sm" if small else ""}">{C.glyph_label(label, 2.8 if small else 3.0)}'
            f'<span>{escape(t["labels"][label])}</span></span>')


def outline_chip(textvalue, cls=""):
    return f'<span class="chip chip--outline {cls}">{escape(textvalue)}</span>'


def compliance_chip(t):
    return f'<span class="chip chip--comply">{C.icon("shield", 3.2, 2)}<span>{escape(t["compliance_short"])}</span></span>'


def screen_foot(t, lang):
    return (f'<footer class="screen-foot"><span>{escape(t["foot"].format(date=long_date(ISSUE["date"])))}</span>'
            f'<span>{escape(t["sample"])}</span></footer>')


def plate(row, t, *, big=True):
    if row["photo"]:
        return (f'<figure class="plate{" plate--big" if big else ""}"><img src="{image(row["photo"])}" alt="{escape(row["title"], quote=True)}">'
                f'<figcaption>{escape(t["photo_note"])}</figcaption></figure>')
    return (f'<figure class="plate plate--empty{" plate--big" if big else ""}">{C.icon(row["category"], 15 if big else 11, 1.1)}'
            f'<figcaption><b>{escape(t["no_photo"])}</b>{"<span>" + escape(t["no_photo_sub"]) + "</span>" if big else ""}</figcaption></figure>')


# --- Cover ------------------------------------------------------------------------------------------

def cover(rows, t, lang):
    counts = ISSUE["counts"]
    sweet = sum(r["label"] == "sweet_spot" for r in rows)
    kpis = [(counts["products"], t["kpi"]["products"]), (counts["new"], t["kpi"]["new"]),
            (counts["updates"], t["kpi"]["updated"]), (sweet, t["kpi"]["sweet"])]
    kpi_html = "".join(f'<div><b>{n}</b><span>{escape(label)}</span></div>' for n, label in kpis)
    tiles = ""
    for r in rows[:6]:
        href = f' href="#p{r["rank"]:02d}"' if r["key"] in SPREADS else ""
        tiles += (f'<a class="mosaic-tile"{href}>{plate(r, t, big=False)}'
                  f'<div class="mosaic-cap"><span class="mosaic-rank">{r["rank"]:02d}</span>'
                  f'<span class="mosaic-title">{escape(r["title"])}</span>'
                  f'<span class="mosaic-score"><b>{r["score"]}</b>{chip(r["label"], t, small=True)}</span></div></a>')
    weekday = ISSUE["weekday_en"] if lang == "en" else ISSUE["weekday_hi"]
    dek = t["count_dek"].format(n=counts["products"], new=counts["new"], upd=counts["updates"], window=window())
    logo = data_url(ASSETS / "images" / "neuraflow-logo-maroon.png", "image/png")
    return f'''<section class="sheet cover" id="cover">
  <div class="cover-inner">
    <div class="cover-top"><img class="logo" src="{logo}" alt="NeuraFlow"><p class="cover-issue">{escape(t["issue_line"].format(weekday=weekday, date=long_date(ISSUE["date"])))}</p></div>
    <div class="cover-title"><p class="kicker">{escape(t["brand_line"])}</p><h1 class="h-display">{escape(ISSUE["title"][lang])}</h1>
    <p class="cover-dek">{escape(dek)}</p></div>
    <div class="cover-kpis">{kpi_html}</div>
    <div class="mosaic">{tiles}</div>
  </div>
  <div class="cover-foot"><p>{escape(t["cover_notice"])}</p><p class="cover-sample">{escape(t["sample"])}</p></div>
</section>'''


# --- At a glance ----------------------------------------------------------------------------------

def stacked(counts, colours, w=52.0, h=4.0):
    total = sum(counts)
    x, body = 0.0, []
    for n, colour in zip(counts, colours):
        seg = w * n / total
        if n:
            body.append(f'<rect x="{x + .25:.2f}" y="0" width="{max(seg - .5, .4):.2f}" height="{h}" fill="{colour}"/>')
        x += seg
    return C.svg(w, h, "".join(body), "stack")


def glance(rows, t, lang):
    counts = ISSUE["counts"]
    comply = sum(r["compliance"] for r in rows)
    kpis = [(counts["products"], t["kpi"]["products"]), (counts["new"], t["kpi"]["new"]),
            (counts["updates"], t["kpi"]["updated"]), (comply, t["kpi"]["compliance"])]
    kpi_html = "".join(f'<div class="kpi"><b>{n}</b><span>{escape(label)}</span></div>' for n, label in kpis)
    labels = ("sweet_spot", "early_bet", "proven_but_crowded")
    lab_counts = [sum(r["label"] == lab for r in rows) for lab in labels]
    lab_legend = "".join(f'<li>{C.glyph_label(lab, 2.8)}<span>{escape(t["labels"][lab])}</span><b>{n}</b></li>'
                         for lab, n in zip(labels, lab_counts))
    crowd_counts = [sum(r["crowd"] == lev for r in rows) for lev in C.LEVELS]
    crowd_legend = "".join(f'<li><i style="background:{C.CROWD[lev]}"></i><span>{escape(t["crowd"][lev])}</span><b>{n}</b></li>'
                           for lev, n in zip(C.LEVELS, crowd_counts))
    cats = {}
    for r in rows:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    top = max(cats.values())
    def cat_bar(n):
        w = 18 * n / top
        return C.svg(w, 2.6, f'<rect width="{w:.2f}" height="2.6" rx=".6" fill="{C.NAVY}"/>', "catbar")
    cat_rows = "".join(f'<li><span>{escape(name)}</span>{cat_bar(n)}<b>{n}</b></li>'
                       for name, n in sorted(cats.items(), key=lambda kv: -kv[1]))
    mixes = f'''<div class="mixes">
  <div class="mix"><p class="mix-title">{escape(t["mix"]["labels"])}</p>{stacked(lab_counts, [C.LABEL[l] for l in labels])}<ul class="mix-legend">{lab_legend}</ul></div>
  <div class="mix"><p class="mix-title">{escape(t["mix"]["crowd"])}</p>{stacked(crowd_counts, [C.CROWD[l] for l in C.LEVELS])}<ul class="mix-legend">{crowd_legend}</ul></div>
  <div class="mix"><p class="mix-title">{escape(t["mix"]["cats"])}</p><ul class="cat-list">{cat_rows}</ul></div>
</div>'''
    head = "".join(f'<th scope="col" class="c{i}">{escape(h)}</th>' for i, h in enumerate(t["table"]))
    body, high_started = "", False
    for r in rows:
        if r["crowd"] == "high" and not high_started:
            high_started = True
            body += f'<tr class="group"><td colspan="6">{escape(t["group_high"])}</td></tr>'
        shield = f'<span class="shield" title="{escape(t["compliance_short"], quote=True)}">{C.icon("shield", 3.0, 2)}</span>' if r["compliance"] else ""
        head_words, last_word = r["title"].rsplit(" ", 1)
        title = escape(head_words) + f' <span class="nw">{escape(last_word)}{"&nbsp;" + shield if shield else ""}</span>'
        shield = ""
        if r["key"] in SPREADS:
            title = f'<a href="#p{r["rank"]:02d}">{title}</a>'
        body += (f'<tr><td class="c0">{r["rank"]:02d}</td>'
                 f'<td class="c1"><span class="pt">{title}{shield}</span><span class="meta">{escape(r["category"])} · {escape(t["status"][r["status"]])}</span></td>'
                 f'<td class="c2"><span class="scorecell">{C.compartment_bar(r["parts"], MAXIMA, label="Score " + str(r["score"]))}<b>{r["score"]}</b></span></td>'
                 f'<td class="c3">{chip(r["label"], t, small=True)}</td>'
                 f'<td class="c4"><span class="crowdcell">{C.crowd_pips(r["crowd"])}<span>{escape(t["crowd"][r["crowd"]])}</span></span></td>'
                 f'<td class="c5">{r["meets"]}<span class="of">/10</span></td></tr>')
    return f'''<section class="sheet glance" id="glance">
  <header class="page-head"><p class="kicker">{escape(t["glance_kicker"])} · {escape(long_date(ISSUE["date"]))}</p><h1 class="h-title">{escape(t["glance_title"])}</h1>
  <p class="dek">{escape(t["glance_dek"].format(n=counts["products"]))}</p></header>
  <div class="kpis">{kpi_html}</div>
  {mixes}
  <table class="league"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>
  <p class="table-key">{escape(t["score_key"])} {C.icon("shield", 3.0, 2)} = {escape(t["compliance_short"].lower() if lang == "en" else t["compliance_short"])}.</p>
  {screen_foot(t, lang)}
</section>'''


# --- How to read (English only in the mock-up) ----------------------------------------------------

def key_page(t):
    demo_parts = {"want": 14, "early": 17, "room": 12, "product": 19}
    labels = "".join(f'<li>{chip(lab, t)}<span>{escape(t["label_meaning"][lab])}</span></li>'
                     for lab in ("sweet_spot", "early_bet", "proven_but_crowded"))
    parts = "".join(f'<li><p><b>{escape(t["parts"][p])}</b> <span class="max">up to {MAXIMA[p]}</span></p><p>{d}</p></li>' for p, d in (
        ("want", "People already show interest: Facebook ads, Google searches, dropshipping-platform lists, quick-commerce stock."),
        ("early", "The interest is new or growing: rising searches, advertisers who started in the last 30 days, new-arrival lists."),
        ("room", "Space left on Amazon and Flipkart. Low crowding earns all 25 points, Medium 12, High none."),
        ("product", "Checklist points met: 5 points earns 10, and each extra point adds 3, up to 25.")))
    states = "".join(f'<li>{C.glyph_state(s, 3.6)}<span><b>{escape(t["states"][s])}</b> {d}</span></li>' for s, d in (
        ("Meets", "The evidence supports this point."), ("Does not meet", "The evidence goes against it."),
        ("Unclear", "Not enough evidence either way.")))
    return f'''<section class="sheet key" id="key">
  <header class="page-head"><p class="kicker">How to read</p><h1 class="h-title">How to read a product page</h1>
  <p class="dek">Every product gets two pages: the product and its score first, then the market and sourcing.</p></header>
  <section class="key-block key-score-block"><h2 class="h-module">The opportunity score</h2>
    <div class="key-score">{C.score_dial(demo_parts, MAXIMA, 62, size=30, caption=t["out_of"], label="Example score 62")}
    <p>The ring is the score out of 100, split into four parts read clockwise from the top. Navy shows points earned; the pale track shows points still missing. Under the title, the same four parts appear as bars with the points behind each one.</p></div>
    <ul class="key-parts">{parts}</ul></section>
  <div class="key-grid">
    <div class="key-col">
      <section class="key-block"><h2 class="h-module">The label</h2><ul class="key-labels">{labels}</ul></section>
      <section class="key-block"><h2 class="h-module">Amazon &amp; Flipkart crowding</h2>
        <div class="key-crowd">{C.crowd_gauge("medium", w=26)}<p>Low, Medium or High, from the similar listings, brands or sellers, ratings and bestseller places we saw. Crowding sets the Room part, and crowded types are listed last.</p></div></section>
      <section class="key-block"><h2 class="h-module">Compliance flag</h2><div class="key-comply">{compliance_chip(t)}<p>Shown on baby, food, skin-contact, medical and electrical items. Check the legal and safety rules yourself before selling.</p></div></section>
    </div>
    <div class="key-col">
      <section class="key-block"><h2 class="h-module">The checklist</h2><ul class="key-states">{states}
        <li><span class="est">{escape(t["estimated"])}</span><span>Judged from indirect evidence, such as listing photos or reviews.</span></li></ul>
        <div class="key-pips">{C.fit_pips(["Meets"] * 7 + ["Unclear", "Does not meet", "Unclear"])}<span>Good fit = 6 to 10 points met. Decent fit = 5.</span></div></section>
      <section class="key-block"><h2 class="h-module">Market signals</h2><ul class="key-signals">
        <li><span class="sig"><i class="dot dot--navy"></i><i class="dot dot--maroon"></i></span><span><b>Meta ads.</b> Each dot is one advertiser, at the date of its first relevant ad. Maroon dots started in the last 30 days.</span></li>
        <li><span class="sig"><i class="band band--sand"></i><i class="band band--rose"></i></span><span><b>Google searches.</b> Weekly interest in India over 12 months. The score compares the last four weeks (rose) with the four before (sand).</span></li>
        <li><span class="sig">{outline_chip("Bestseller", "chip--role")}</span><span><b>Dropshipping platforms.</b> Whether DeoDap, BaapStore or Dropdash list the type, on which list, and at what dropship price.</span></li>
        <li><span class="sig">{C.glyph_state("instock", 3.4)}{C.glyph_state("nostock", 3.4)}{C.glyph_state("unknown", 3.4)}</span><span><b>Quick commerce.</b> In stock, not in stock, or not checked, per app and city.</span></li></ul></section>
      <section class="key-block"><h2 class="h-module">Supplier leads</h2><p class="key-p">One row per lead: where it is, how we found it, the minimum order and five terms, each {C.glyph_state("yes", 3.2)} yes, {C.glyph_state("no", 3.2)} no or {C.glyph_state("unknown", 3.2)} not stated. Leads are starting points, not recommendations.</p></section>
    </div>
  </div>
  <p class="key-not-line">This report does not predict sales, profit, margin or return-to-origin (RTO) rates. Suppliers are leads, not endorsements. Full notes are on the last page.</p>
  {screen_foot(t, "en")}
</section>'''


# --- Product page one -------------------------------------------------------------------------------

def product_a(row, spread, t, lang):
    rank = f'{row["rank"]:02d}'
    criteria = spread["criteria"]
    states = [c[1] for c in criteria]
    tier = "good" if row["meets"] >= 6 else "decent"
    chips = chip(row["label"], t) + outline_chip(t["tier"][tier], "chip--fit")
    banner = ""
    if spread["compliance"]:
        banner = (f'<div class="comply">{C.icon("shield", 4.4, 1.8)}<p><b>{escape(t["compliance_title"])}.</b> '
                  f'{escape(spread["compliance"][lang])}</p></div>')
    cols = ""
    for part in ("want", "early", "room", "product"):
        items = " · ".join(f'{escape(t["items"][k])}&nbsp;<b>{row["items"][k]}</b>' for k in ITEMS[part])
        if part == "room":
            items = f'{escape(t["items"]["crowding"])}: <b>{escape(t["crowd"][row["crowd"]])}</b>'
        if part == "product":
            items = " · ".join(f'{escape(t["states"][st])}&nbsp;<b>{states.count(st)}</b>' for st in ("Meets", "Unclear", "Does not meet") if states.count(st))
        cols += (f'<li class="part"><p class="part-head"><b>{escape(t["parts"][part])}</b>'
                 f'<span class="part-val"><b>{row["parts"][part]}</b>/{MAXIMA[part]}</span></p>'
                 f'{C.part_bar(row["parts"][part], MAXIMA[part], scale=38.0 / MAXIMA[part], h=2.2)}'
                 f'<p class="part-items">{items}</p></li>')
    points = "".join(f'<li>{escape(p)}</li>' for p in spread["points"][lang])
    notes = f'<div class="note"><p class="note-label">{escape(t["keep"])}</p><p>{escape(spread["limitation"][lang])}</p></div>'
    changed = ""
    if spread["update"]:
        changed = f'<p class="changed"><b>{escape(t["changed"])}:</b> {escape(spread["update"][lang])}</p>'
    fit_items = ""
    for cid, state, est, en, hi in criteria:
        reason = en if lang == "en" else hi
        est_tag = f' <span class="est">{escape(t["estimated"])}</span>' if est else ""
        state_cls = "crit-state" if state != "Meets" else "crit-state sr"
        fit_items += (f'<li>{C.glyph_state(state, 3.8)}<div><p class="crit"><b>{escape(CRITERIA[cid])}</b>'
                      f'<span class="{state_cls}">{escape(t["states"][state])}</span>{est_tag}</p><p class="crit-why">{escape(reason)}</p></div></li>')
    kicker = f'{escape(row["category"])} · {escape(t["status_long"][row["status"]])}'
    return f'''<section class="sheet product-a" id="p{rank}">
  <header class="pa-head">
    <div class="pa-photo">{plate(row, t)}<span class="rank-badge">{rank}</span></div>
    <div class="pa-titles"><p class="kicker">{kicker}</p><h1 class="h-title">{escape(row["title"])}</h1><div class="chips">{chips}</div>
      <p class="label-line">{C.glyph_label(row["label"], 3.0)}<span>{escape(t["label_meaning"][row["label"]])}</span></p>{changed}</div>
    <div class="pa-dial">{C.score_dial(row["parts"], MAXIMA, row["score"], size=30, caption=t["out_of"], label=t["score"] + " " + str(row["score"]))}<p>{escape(t["score"])}</p></div>
  </header>
  {banner}
  <section class="adds-up"><h2 class="h-module">{escape(t["adds_up"])}</h2><ul class="parts">{cols}</ul></section>
  <section class="why"><h2 class="h-section">{escape(t["why"])}</h2><p class="lead">{escape(spread["why"][lang])}</p>
    <ol class="points">{points}</ol><div class="notes">{notes}</div></section>
  <section class="fit"><div class="fit-head"><h2 class="h-section">{escape(t["fit"])}</h2>
    <p class="fit-sum">{C.fit_pips(states)}<span><b>{escape(t["tier"][tier])}</b> · {escape(t["fit_count"].format(m=row["meets"]))}</span></p></div>
    <ul class="fit-list">{fit_items}</ul></section>
  {screen_foot(t, lang)}
</section>'''


# --- Product page two -------------------------------------------------------------------------------

def product_b(row, spread, t, lang):
    rank = f'{row["rank"]:02d}'
    f = spread["crowd_facts"]
    nobrand = f' <span class="soft">({escape(t["c_nobrand"].format(n=f["no_brand"]))})</span>' if f["no_brand"] else ""
    crowd = f'''<article class="tile tile--crowd"><header><p class="tile-title">{escape(t["t_crowd"])}</p><p class="tile-src">{escape(t["t_crowd_src"].format(d="16–17 Oct"))}</p></header>
  <div class="crowd-body"><div class="crowd-left">{C.crowd_gauge(row["crowd"], w=30, low=t["crowd"]["low"], high=t["crowd"]["high"])}<p class="stat">{escape(t["crowd"][row["crowd"]])}</p><p class="soft">{escape(t["c_room"].format(p=row["parts"]["room"]))}</p></div>
  <dl class="facts"><div><dt>{escape(t["c_listings"])}</dt><dd>Amazon {f["amazon"]} · Flipkart {f["flipkart"]}</dd></div>
  <div><dt>{escape(t["c_brands"])}</dt><dd>{f["brands"]}{nobrand}</dd></div>
  <div><dt>{escape(t["c_ratings"])}</dt><dd>{C.fmt_in(f["max_ratings"])}</dd></div>
  <div><dt>{escape(t["c_best"])}</dt><dd>{f["bestseller"]}</dd></div></dl></div></article>'''
    m = spread["meta"]
    meta = f'''<article class="tile tile--meta"><header><p class="tile-title">{escape(t["t_meta"])}</p><p class="tile-src">{escape(t["t_meta_src"].format(d="15 Oct"))}</p></header>
  <p class="stat-line"><span class="stat">{m["advertisers"]}</span> <span>{escape(t["m_adv"])}</span><span class="soft">{escape(t["m_ads"].format(r=m["relevant"], a=m["active"]))}</span></p>
  {C.meta_timeline(m["starts"], m["observed"])}
  <p class="legend"><span><i class="dot dot--navy"></i>{escape(t["m_older"])}</span><span><i class="dot dot--maroon"></i>{escape(t["m_recent"])}</span></p></article>'''
    tr = spread["trends"]
    state_cls = {"rising": "up", "rising_strongly": "up", "steady": "flat"}[tr["state"]]
    trends = f'''<article class="tile tile--trends"><header><p class="tile-title">{escape(t["t_trends"])}</p><p class="tile-src">{escape(t["t_trends_src"].format(d="14 Oct"))}</p></header>
  <p class="trend-state trend-state--{state_cls}">{escape(t["tr_state"][tr["state"]])}</p>
  {C.trends_spark(tr["series"])}
  <p class="tile-note">{escape(t["tr_cap"].format(r=f"{tr['recent'][0]}–{tr['recent'][1]}", p=f"{tr['previous'][0]}–{tr['previous'][1]}"))}</p></article>'''
    plat_rows = ""
    for name, state, roles, price in spread["platforms"]:
        role_html = "".join(outline_chip(r, "chip--role") for r in roles)
        price_html = f'<span class="p-price">₹{price}</span>' if price else '<span class="p-price soft">–</span>'
        plat_rows += (f'<li><span class="p-name">{escape(name)}</span><span class="p-state">{C.glyph_state(state, 3.0)}{escape(t["p_state"][state])}</span>'
                      f'{price_html}<span class="p-roles">{role_html}</span></li>')
    plats = f'''<article class="tile tile--plat"><header><p class="tile-title">{escape(t["t_plat"])}</p><p class="tile-src">{escape(t["t_plat_src"].format(d="13–15 Oct"))}</p></header>
  <ul class="plat">{plat_rows}</ul><p class="tile-note">₹ = {escape(t["p_price"])}</p></article>'''
    qc_states = [s for states in spread["qc"].values() for s in states]
    cities_in = sum(any(states[i] == "yes" for states in spread["qc"].values()) for i in range(len(spread["qc_cities"])))
    qc = f'''<article class="tile tile--qc"><header><p class="tile-title">{escape(t["t_qc"])}</p><p class="tile-src">{escape(t["t_qc_src"].format(d="18 Oct"))}</p></header>
  {C.qc_matrix(spread["qc_cities"], spread["qc"], labels=t["qc"])}
  <p class="tile-note"><b>{escape(t["qc_sum"].format(k=cities_in, n=len(spread["qc_cities"])))}</b></p><p class="legend legend--qc"><span>{C.glyph_state("instock", 2.6)}{escape(t["qc"]["yes"])}</span><span>{C.glyph_state("nostock", 2.6)}{escape(t["qc"]["no"])}</span><span>{C.glyph_state("unknown", 2.6)}{escape(t["qc"]["unchecked"])}</span></p>
</article>'''
    del qc_states

    def price_list(items):
        return "".join(f'<li><b class="price">₹{C.fmt_in(p)}</b><span class="p-what">{escape(what)}</span>'
                       f'<span class="soft">{escape(src)} · {escape(t["seen"].format(d=d))}</span></li>' for p, what, src, d in items)
    prices = f'''<section class="prices-sec"><h2 class="h-module">{escape(t["prices"])}</h2><div class="prices">
  <div><p class="sub">{escape(t["retail"])}</p><ul class="price-list">{price_list(spread["prices"]["retail"])}</ul></div>
  <div><p class="sub">{escape(t["supplier"])}</p><ul class="price-list">{price_list(spread["prices"]["supplier"])}</ul></div></div>
  <p class="bulk">{escape(spread["bulk"][lang])}</p></section>'''
    term_keys = ("dispatch", "cod", "plain", "label", "fees")
    sup_head = (f'<th scope="col" class="s-name">{escape(t["suppliers"])}</th><th scope="col">{escape(t["moq"])}</th>'
                + "".join(f'<th scope="col">{escape(t["terms"][k])}</th>' for k in term_keys))
    sup_rows = ""
    for s in spread["suppliers"]:
        cells = "".join(f'<td data-term="{escape(t["terms"][k], quote=True)}"><span class="term">{C.glyph_state(s["terms"][k], 3.2)}{escape(t["tv"][s["terms"][k]])}</span></td>'
                        for k in term_keys)
        sup_rows += (f'<tr><th scope="row" class="s-name"><b>{escape(s["name"])}</b><span>{escape(t["no_city"] if s["place"] == "City not stated" else s["place"])} · {escape(t["via"])} {escape(s["via"])}</span></th>'
                     f'<td class="s-moq" data-term="{escape(t["moq"], quote=True)}"><b>{escape(s["moq"])}</b></td>{cells}</tr>')
    suppliers = (f'<section class="sup-sec"><table class="sup"><thead><tr>{sup_head}</tr></thead><tbody>{sup_rows}</tbody></table>'
                 f'<p class="sup-note">{escape(t["sup_note"])}</p></section>')
    ex_rows = "".join(f'<li><b>{escape(ex[0])}</b><span class="ex-price">{escape(ex[4])}</span>'
                      f'<span class="soft">{escape(ex[1])} · {escape(ex[2])} · {escape(ex[3])} · {escape(t["seen"].format(d=ex[5]))}</span></li>'
                      for ex in spread["examples"])
    examples = (f'<section class="ex-sec"><h2 class="h-module">{escape(t["examples"])}</h2><ul class="ex-list">{ex_rows}</ul>'
                f'<p class="tile-note">{escape(t["ex_note"])}</p></section>')
    sources = "".join(f'<li><a href="#">{escape(name)}</a> <span>{escape(d)}</span></li>' for name, d in spread["sources"])
    return f'''<section class="sheet product-b">
  <header class="pb-head"><div class="pb-strip"><span class="pb-rank">{rank}</span><h2 class="pb-title">{escape(row["title"])}<span class="pb-sec">{escape(t["market"])}</span></h2>{chip(row["label"], t, small=True)}<span class="pb-score"><b>{row["score"]}</b>/100</span></div></header>
  <div class="tiles tiles--2">{crowd}{meta}</div>
  <div class="tiles tiles--3">{trends}{plats}{qc}</div>
  <div class="pb-bottom">{prices}{examples}</div>
  {suppliers}
  <section class="src-sec"><h2 class="h-module">{escape(t["sources"])}</h2><ul class="sources">{sources}</ul></section>
  {screen_foot(t, lang)}
</section>'''


# --- Closing page (English only in the mock-up) ----------------------------------------------------

def closing(t):
    steps = [("Find", "We search the agreed websites for narrow product types in four categories: Home and Kitchen, Home and Decor, Baby Products, and Travel and Outdoor."),
             ("Check", "Each type is judged against the 10-point checklist. We record what Meta ads, Google Trends, dropshipping platforms, quick-commerce apps, Amazon and Flipkart show."),
             ("Score", "Four parts add up to the opportunity score out of 100: Want (20), Early (30), Room (25) and Product (25)."),
             ("Shortlist", "Types are ranked by score, with crowded ones last. We look for supplier leads and keep dated links to everything we saw.")]
    step_html = "".join(f'<li><span class="step-n">{i}</span><p class="step-t">{escape(a)}</p><p>{escape(b)}</p></li>' for i, (a, b) in enumerate(steps, 1))
    sites = ["Facebook Ads Library", "Google Trends India", "Amazon India", "Flipkart", "DeoDap", "BaapStore", "Dropdash",
             "Blinkit", "Zepto", "Instamart", "IndiaMART", "Google Maps supplier search", "Supplier company pages"]
    site_html = "".join(f'<li>{escape(s)}</li>' for s in sites)
    counts = ISSUE["counts"]
    return f'''<section class="sheet closing" id="method">
  <header class="page-head"><p class="kicker">Method and notes</p><h1 class="h-title">How each issue is made</h1>
  <p class="dek">The same four steps run for every issue, three times a week.</p></header>
  <ol class="steps">{step_html}</ol>
  <div class="close-grid">
    <section><h2 class="h-module">Sites checked for this issue</h2><ul class="sites">{site_html}</ul></section>
    <section><h2 class="h-module">This issue</h2><dl class="issue-facts">
      <div><dt>Issue date</dt><dd>{escape(long_date(ISSUE["date"]))}</dd></div>
      <div><dt>Research window</dt><dd>{escape(window())}</dd></div>
      <div><dt>Product types</dt><dd>{counts["products"]} ({counts["new"]} new, {counts["updates"]} updated)</dd></div>
      <div><dt>Languages</dt><dd>English and Roman Hinglish, same facts</dd></div>
      <div><dt>Workbook</dt><dd>Every row behind this report, in Excel</dd></div></dl></section>
  </div>
  <aside class="notes-box"><h2 class="h-module">Important notes</h2><ul>
    <li>This report suggests products to test. It does not predict sales, profit, margin or return-to-origin (RTO) rates, and a high score is not a promise that a product will sell.</li>
    <li>Prices, stock and supplier terms are what we saw on the dates shown. They can change at any time.</li>
    <li>Supplier leads are starting points only. NeuraFlow has not contacted, checked or endorsed them.</li>
    <li>Items marked “Client compliance check required” need your own check of legal, safety and labelling rules before you sell them.</li>
    <li>“Estimated” marks a checklist answer judged from indirect evidence such as listing photos or reviews.</li>
    <li>Google Trends shows relative interest (0–100), not the number of searches or sales.</li></ul></aside>
  <p class="sample-note">This mock-up uses invented sample figures, fictional supplier names and NeuraFlow’s illustrative still-life images. It is not research.</p>
  {screen_foot(t, "en")}
</section>'''


# --- Stylesheet -------------------------------------------------------------------------------------

CSS = r"""
:root{
  --paper:#F9F3EC;--paper-deep:#EEE5DB;--card:#FFFCF7;--plate:#FFFFFF;--line:#D8D0C8;--line-soft:#E7DFD6;
  --ink:#292C2F;--ink-2:#5F6062;--navy:#24384B;--navy-2:#647487;--track:#DCE3EA;
  --maroon:#6B1F2B;--maroon-pale:#F1DFE2;
  --sweet:#2A6CA3;--sweet-pale:#E3ECF5;--early:#B07A12;--early-pale:#F3E7C9;--crowded:#9A2F3E;--crowded-pale:#F1DFE2;
  --serif:"NF Serif",Georgia,serif;--head:"NF Serif Head","NF Serif",Georgia,serif;
  --sans:"NF Sans","Segoe UI",Arial,sans-serif;--cond:"NF Sans Cond","Arial Narrow",sans-serif;
  --fs-micro:8pt;--fs-kicker:8.5pt;--fs-label:9pt;--fs-strong:9.5pt;--fs-note:10pt;--fs-body:10.5pt;--fs-lead:11.5pt;
  --fs-module:12.5pt;--fs-section:14pt;--fs-stat:15.5pt;--fs-title:18pt;--fs-kpi:20pt;--fs-display:28pt;
}
@page{size:A4 portrait;margin:15mm 16mm 17mm 16mm;background:#F9F3EC;
  @top-right{content:"SAMPLE DATA · DESIGN MOCK-UP";font-family:"NF Sans Cond";font-weight:600;font-size:8pt;letter-spacing:.08em;color:#6B1F2B;vertical-align:bottom;padding-bottom:4mm}
  @bottom-left{content:var(--foot-text, "NeuraFlow · Product Opportunity Brief");font-family:"NF Sans";font-size:8pt;color:#5F6062;vertical-align:top;padding-top:6mm}
  @bottom-right{content:"Page " counter(page) " of " counter(pages);font-family:"NF Sans";font-size:8pt;color:#5F6062;vertical-align:top;padding-top:6mm}}
@page cover{margin:0;@top-right{content:none}@bottom-left{content:none}@bottom-right{content:none}}
*{box-sizing:border-box}
html{background:var(--paper)}
body{margin:0;color:var(--ink);font-family:var(--sans);font-size:var(--fs-label);line-height:1.4;font-kerning:normal;
  text-rendering:optimizeLegibility;-webkit-print-color-adjust:exact;print-color-adjust:exact}
h1,h2,h3,p,ul,ol,dl,dd,figure{margin:0;padding:0}
ul,ol{list-style:none}
a{color:inherit;text-decoration:none}
b,strong{font-weight:600}
svg{display:block;overflow:visible}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.soft{color:var(--ink-2)}

/* type roles */
.kicker{font-family:var(--cond);font-weight:600;font-size:var(--fs-kicker);letter-spacing:.08em;text-transform:uppercase;color:var(--maroon);line-height:1.25;margin-bottom:1.6mm}
.h-display{font-family:var(--head);font-weight:600;font-size:var(--fs-display);line-height:1.08;color:var(--navy);letter-spacing:-.005em;text-wrap:balance;hyphens:none}
.h-title{font-family:var(--head);font-weight:600;font-size:var(--fs-title);line-height:1.15;color:var(--navy);text-wrap:balance;hyphens:none}
.h-section{font-family:var(--head);font-weight:600;font-size:var(--fs-section);line-height:1.2;color:var(--navy);margin-bottom:2mm;break-after:avoid}
.h-module{font-family:var(--sans);font-weight:600;font-size:var(--fs-strong);line-height:1.3;color:var(--navy);margin-bottom:2mm;break-after:avoid}
.dek{font-family:var(--serif);font-size:var(--fs-lead);line-height:1.45;color:var(--ink);max-width:150mm;margin-top:2.4mm}
.stat{font-family:var(--sans);font-weight:600;font-size:var(--fs-stat);line-height:1.1;color:var(--navy)}

/* chips */
.chip{display:inline-flex;align-items:center;gap:1.3mm;height:5.6mm;padding:0 2.4mm 0 1.8mm;border-radius:2.8mm;font-family:var(--sans);font-weight:600;font-size:var(--fs-label);color:var(--ink);white-space:nowrap;line-height:1}
.chip--sm{height:4.8mm;padding:0 2mm 0 1.5mm;font-size:var(--fs-micro);gap:1.1mm}
.chip--sweet_spot{background:var(--sweet-pale)}.chip--early_bet{background:var(--early-pale)}.chip--proven_but_crowded{background:var(--crowded-pale)}
.chip--outline{background:transparent;box-shadow:inset 0 0 0 .25mm var(--line);padding:0 2.4mm;font-weight:500}
.chip--role{height:4.4mm;padding:0 1.6mm;font-size:var(--fs-micro);font-weight:500;color:var(--navy);box-shadow:inset 0 0 0 .25mm var(--navy-2)}
.chip--comply{background:transparent;color:var(--maroon);box-shadow:inset 0 0 0 .3mm var(--maroon)}
.chip--comply .icon{color:var(--maroon)}
.glyph{flex:none}

/* screen preview (desktop) and print */
@media screen{
  html{background:#CFC9C2}body{padding:8mm 0}
  .sheet{position:relative;width:210mm;min-height:297mm;margin:0 auto 8mm;padding:15mm 16mm 17mm;background:var(--paper);box-shadow:0 .4mm 1.2mm rgba(0,0,0,.10),0 3mm 9mm rgba(0,0,0,.08)}
  .sheet.cover{padding:0;height:297mm}
  .screen-foot{position:absolute;left:16mm;right:16mm;bottom:7mm;display:flex;justify-content:space-between;font-size:var(--fs-micro);color:var(--ink-2)}
}
@media print{
  .screen-foot{display:none}
  .sheet{break-after:page}.sheet:last-child{break-after:auto}
  .cover{page:cover;height:297mm;overflow:hidden}
}

/* cover */
.cover{display:flex;flex-direction:column;background:var(--paper)}
.cover-inner{padding:15mm 16mm 0;flex:1}
.cover-top{display:flex;justify-content:space-between;align-items:center}
.logo{width:42mm;height:auto}
.cover-issue{font-family:var(--cond);font-weight:600;font-size:var(--fs-kicker);letter-spacing:.08em;text-transform:uppercase;color:var(--maroon)}
.cover-title{margin-top:16mm;max-width:156mm}
.cover-title .kicker{margin-bottom:3mm}
.cover-dek{font-family:var(--serif);font-size:var(--fs-lead);line-height:1.5;margin-top:4mm;max-width:150mm}
.cover-kpis{display:grid;grid-template-columns:repeat(4,1fr);margin-top:8mm;border-top:.3mm solid var(--line);border-bottom:.3mm solid var(--line)}
.cover-kpis div{padding:3.2mm 0 3.4mm 4mm;border-left:.3mm solid var(--line-soft)}
.cover-kpis div:first-child{padding-left:0;border-left:0}
.cover-kpis b,.kpi b{display:block;font-weight:600;font-size:var(--fs-kpi);line-height:1.1;color:var(--navy)}
.cover-kpis span,.kpi span{font-size:var(--fs-label);color:var(--ink-2)}
.mosaic{display:grid;grid-template-columns:repeat(3,1fr);gap:5mm 4mm;margin-top:8mm}
.mosaic-tile{display:block}
.mosaic .plate{height:50mm}
.mosaic .plate figcaption{display:none}
.mosaic .plate--empty figcaption{display:block}
.mosaic-cap{display:grid;grid-template-columns:auto 1fr;column-gap:1.8mm;row-gap:1.2mm;margin-top:2.2mm;align-items:baseline}
.mosaic-rank{font-family:var(--cond);font-weight:600;font-size:var(--fs-strong);color:var(--maroon)}
.mosaic-title{font-weight:600;font-size:var(--fs-label);line-height:1.3;color:var(--ink)}
.mosaic-score{grid-column:2;display:flex;align-items:center;gap:1.8mm}
.mosaic-score b{font-size:var(--fs-strong);color:var(--navy)}
.cover-foot{background:var(--navy);color:#EFE8DF;padding:6mm 16mm 7mm;display:flex;justify-content:space-between;align-items:flex-end;gap:10mm;font-size:var(--fs-label);line-height:1.45}
.cover-foot p:first-child{max-width:120mm}
.cover-sample{font-family:var(--cond);font-weight:600;font-size:var(--fs-micro);letter-spacing:.08em;text-transform:uppercase;color:#E9C9CE;white-space:nowrap}

/* plate (photo) */
.plate{position:relative;background:var(--plate);border-radius:2mm;display:flex;align-items:center;justify-content:center;padding:2.5mm;
  border:.25mm solid var(--line-soft)}
.plate img{max-width:100%;max-height:100%;object-fit:contain;border-radius:1mm}
.plate figcaption{position:absolute;left:0;bottom:-4.6mm;font-size:var(--fs-micro);color:var(--ink-2)}
.plate--empty{background:var(--paper-deep);border-color:transparent;flex-direction:column;gap:2.5mm;color:var(--navy-2);text-align:center}
.plate--empty figcaption{position:static;color:var(--ink-2);font-size:var(--fs-micro);line-height:1.35;padding:0 3mm}
.plate--empty figcaption b{display:block;font-weight:600;color:var(--navy-2)}
.plate--big{height:64mm}
.plate--big.plate--empty figcaption{font-size:var(--fs-label)}

/* page head */
.page-head{margin-bottom:5mm}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);border-top:.3mm solid var(--line);border-bottom:.3mm solid var(--line);margin-bottom:5mm}
.kpi{padding:2.6mm 0 2.8mm 4mm;border-left:.3mm solid var(--line-soft)}
.kpi:first-child{padding-left:0;border-left:0}

/* mixes */
.mixes{display:grid;grid-template-columns:1fr 1fr 1fr;gap:7mm;margin-bottom:5mm}
.mix-title{font-weight:600;font-size:var(--fs-label);color:var(--navy);margin-bottom:1.8mm}
.mix .stack{margin-bottom:2mm}
.mix-legend li{display:flex;align-items:center;gap:1.6mm;font-size:var(--fs-label);line-height:1.6}
.mix-legend li span{flex:1}
.mix-legend li b{font-variant-numeric:tabular-nums}
.mix-legend i{display:inline-block;width:2.8mm;height:2.8mm;border-radius:.4mm}
.cat-list li{display:grid;grid-template-columns:31mm 1fr auto;align-items:center;gap:1.6mm;font-size:var(--fs-label);line-height:1.6}
.cat-list b{font-variant-numeric:tabular-nums}

/* league table */
.league{width:100%;border-collapse:collapse;table-layout:fixed}
.league thead th{font-family:var(--cond);font-weight:600;font-size:var(--fs-micro);letter-spacing:.06em;text-transform:uppercase;color:var(--ink-2);text-align:left;padding:0 2mm 1.6mm 0;border-bottom:.3mm solid var(--line)}
.league td{padding:1.5mm 2mm 1.5mm 0;border-bottom:.25mm solid var(--line-soft);vertical-align:middle}
.league tr{break-inside:avoid}
.league .c0{width:8mm;font-family:var(--cond);font-weight:600;font-size:var(--fs-strong);color:var(--maroon)}
.league .c1{width:auto}.league .c2{width:38mm}.league .c3{width:36mm}.league .c4{width:23mm}.league .c5{width:11mm;text-align:right;padding-right:0}
.league .chip--sm{white-space:normal;height:auto;min-height:4.8mm;padding-top:.7mm;padding-bottom:.7mm;line-height:1.2;border-radius:2.4mm}
.league thead .c5{text-align:right;padding-right:0}
.league .pt{display:block;font-weight:600;font-size:var(--fs-strong);line-height:1.25;color:var(--ink)}
.league .pt a{border-bottom:.25mm solid rgba(36,56,75,.35)}
.league .meta{display:block;font-size:var(--fs-micro);color:var(--ink-2);margin-top:.4mm}
.league .shield{color:var(--maroon);display:inline-block;vertical-align:-.5mm}
.nw{white-space:nowrap}
.league .shield .icon{display:inline-block}
.scorecell{display:flex;align-items:center;gap:2.2mm}
.scorecell b{font-size:var(--fs-strong);color:var(--navy);font-variant-numeric:tabular-nums}
.crowdcell{display:flex;align-items:center;gap:1.6mm;font-size:var(--fs-label)}
.league .c5{font-weight:600;font-size:var(--fs-strong);color:var(--navy);font-variant-numeric:tabular-nums}
.league .of{font-weight:400;font-size:var(--fs-label);color:var(--ink-2)}
.league tr.group td{padding:3.2mm 0 1.2mm;border-bottom:.3mm solid var(--line);font-family:var(--cond);font-weight:600;font-size:var(--fs-micro);letter-spacing:.06em;text-transform:uppercase;color:var(--maroon)}
.table-key{display:flex;flex-wrap:wrap;align-items:center;gap:1mm;margin-top:2.6mm;font-size:var(--fs-micro);color:var(--ink-2)}
.table-key .icon{color:var(--maroon)}

/* product page one */
.pa-head{display:grid;grid-template-columns:50mm 1fr 30mm;column-gap:6mm;align-items:start;padding-bottom:3.6mm;border-bottom:.3mm solid var(--line);margin-bottom:3.4mm}
.pa-photo{position:relative}
.pa-photo .plate--big{height:50mm}
.pa-photo .plate figcaption{left:auto;right:1.6mm;bottom:1.4mm;padding:.4mm 1.4mm;border-radius:.8mm;background:rgba(255,255,255,.88)}
.pa-photo .plate--empty figcaption{position:static;background:none}
.rank-badge{position:absolute;left:0;top:0;min-width:10mm;height:8mm;padding:0 2mm;display:flex;align-items:center;justify-content:center;background:var(--maroon);color:#fff;font-family:var(--cond);font-weight:600;font-size:var(--fs-module);border-radius:2mm 0 2mm 0}
.pa-titles{padding-top:.6mm}
.chips{display:flex;flex-wrap:wrap;gap:1.6mm;margin-top:3mm}
.pa-dial{display:flex;flex-direction:column;align-items:center;gap:1.2mm}
.pa-dial p{font-weight:600;font-size:var(--fs-label);color:var(--navy);text-align:center}
.label-line{display:flex;gap:2mm;align-items:flex-start;font-size:var(--fs-label);line-height:1.4;margin-top:3mm;max-width:85mm}
.label-line .glyph{margin-top:.5mm}
.changed{margin-top:2.4mm;padding-left:2.6mm;border-left:.6mm solid var(--maroon);font-size:var(--fs-label);line-height:1.4;max-width:88mm}
.changed b{color:var(--maroon)}
.adds-up{margin-bottom:3.6mm;padding-top:1mm}
.parts{display:grid;grid-template-columns:repeat(4,1fr);gap:5mm}
.part-head{display:flex;justify-content:space-between;align-items:baseline;gap:2mm;font-size:var(--fs-label);margin-bottom:1.2mm}
.part-head>b{color:var(--navy);font-size:var(--fs-strong)}
.part-val{white-space:nowrap;color:var(--ink-2);font-variant-numeric:tabular-nums}
.part-val b{color:var(--navy);font-size:var(--fs-strong)}
.part-mean{font-size:var(--fs-micro);color:var(--ink);margin-top:1.3mm;line-height:1.35}
.part-items{font-size:var(--fs-micro);color:var(--ink-2);margin-top:1.3mm;line-height:1.4}
.part-items b{color:var(--ink);font-weight:600}
.comply{display:grid;grid-template-columns:4.4mm 1fr;gap:2.6mm;align-items:start;padding:2.2mm 3.6mm;margin:0 0 3.6mm;background:var(--maroon-pale);border-left:1mm solid var(--maroon);border-radius:0 1.6mm 1.6mm 0;color:var(--maroon)}
.comply p{font-size:var(--fs-label);line-height:1.45;color:var(--ink)}
.comply b{font-weight:600;color:var(--maroon)}
.why{margin-bottom:3.8mm}
.lead{font-family:var(--serif);font-size:var(--fs-body);line-height:1.47;max-width:168mm}
.points{counter-reset:pt;margin-top:2mm;display:grid;gap:1.2mm;max-width:168mm}
.points li{counter-increment:pt;display:grid;grid-template-columns:6mm 1fr;font-family:var(--serif);font-size:var(--fs-note);line-height:1.45}
.points li::before{content:counter(pt);font-family:var(--cond);font-weight:600;font-size:var(--fs-strong);color:var(--maroon);line-height:1.6}
.notes{display:grid;grid-template-columns:1fr;gap:3mm;margin-top:3mm}
.notes:has(.note--update){grid-template-columns:1fr 1fr;gap:6mm}
.note{padding-top:2.4mm;border-top:.3mm solid var(--line)}
.note-label{font-family:var(--cond);font-weight:600;font-size:var(--fs-kicker);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-2);margin-bottom:1mm}
.note p+p,.note p:last-child{font-family:var(--serif);font-style:italic;font-size:var(--fs-note);line-height:1.45}
.note .note-label{font-style:normal;font-family:var(--cond)}
.note--update{border-top-color:var(--maroon)}
.note--update .note-label{color:var(--maroon)}
.fit-head{display:flex;justify-content:space-between;align-items:baseline;gap:4mm;padding-top:2.8mm;border-top:.3mm solid var(--line);margin-bottom:2mm}
.fit-head .h-section{margin-bottom:0}
.fit-sum{display:flex;align-items:center;gap:2.4mm;font-size:var(--fs-label);color:var(--ink-2)}
.fit-sum b{color:var(--navy)}
.fit-list{columns:2;column-gap:6.5mm}
.fit-list li{break-inside:avoid;display:grid;grid-template-columns:3.8mm 1fr;gap:2.2mm;margin-bottom:1.35mm}
.fit-list .glyph{margin-top:.3mm}
.crit{display:flex;flex-wrap:wrap;align-items:baseline;gap:0 1.6mm;font-size:var(--fs-strong);line-height:1.3}
.crit b{color:var(--ink)}
.crit-state{font-size:var(--fs-label);color:var(--maroon)}
.est{display:inline-block;font-size:var(--fs-micro);line-height:1;padding:.7mm 1.3mm;border-radius:1mm;box-shadow:inset 0 0 0 .25mm var(--navy-2);color:var(--navy-2);font-weight:500}
.crit-why{font-size:var(--fs-label);line-height:1.4;color:var(--ink);margin-top:.4mm}

/* product page two */
.pb-head{margin-bottom:3.4mm}
.pb-strip{display:flex;align-items:center;gap:2.6mm;padding-bottom:2.6mm;border-bottom:.3mm solid var(--line)}
.pb-rank{font-family:var(--cond);font-weight:600;font-size:var(--fs-module);color:var(--maroon)}
.pb-title{font-family:var(--head);font-weight:600;font-size:var(--fs-module);color:var(--navy);flex:1}
.pb-sec{margin-left:2.6mm;font-family:var(--cond);font-weight:600;font-size:var(--fs-kicker);letter-spacing:.08em;text-transform:uppercase;color:var(--maroon);vertical-align:.4mm}
.pb-score{font-size:var(--fs-label);color:var(--ink-2);white-space:nowrap}
.pb-score b{font-size:var(--fs-module);color:var(--navy)}
.sec-head{display:flex;justify-content:space-between;align-items:baseline;gap:4mm;margin-bottom:2.6mm}
.sec-head .h-section{margin-bottom:0}
.sec-head .soft{font-size:var(--fs-micro)}
.tiles{display:grid;gap:3.2mm;margin-bottom:3.2mm}
.tiles--2{grid-template-columns:1fr 1fr}.tiles--3{grid-template-columns:1fr 1fr 1fr}
.tile{background:var(--card);border-radius:2mm;padding:2.8mm 3.4mm 3mm;border:.25mm solid var(--line-soft);break-inside:avoid}
.tile-title{font-weight:600;font-size:var(--fs-strong);color:var(--navy);line-height:1.3}
.tile-src{font-size:var(--fs-micro);color:var(--ink-2);margin:.2mm 0 2mm}
.tile-note{font-size:var(--fs-micro);color:var(--ink-2);line-height:1.45;margin-top:1.6mm}
.tile-note .glyph{display:inline-block;vertical-align:-.5mm}
.crowd-body{display:grid;grid-template-columns:31mm 1fr;gap:4mm;align-items:start}
.crowd-left .stat{margin-top:1.2mm}
.crowd-left .soft{font-size:var(--fs-micro);margin-top:.4mm}
.facts{display:grid;grid-template-columns:1fr;gap:1.5mm;padding-left:4mm;border-left:.25mm solid var(--line-soft)}
.facts>div{font-size:var(--fs-label);line-height:1.35}
.facts dt,.facts dd{display:inline}
.facts dt::after{content:": "}
.facts dt{font-size:var(--fs-label);color:var(--ink-2)}
.facts dd{font-weight:600;font-size:var(--fs-label);color:var(--ink);font-variant-numeric:tabular-nums}
.facts dd .soft{font-weight:400}
.stat-line{display:flex;align-items:baseline;flex-wrap:wrap;gap:1.4mm;margin-bottom:1.6mm;font-size:var(--fs-label)}
.stat-line .soft{flex-basis:100%;font-size:var(--fs-label)}
.legend{display:flex;flex-wrap:wrap;gap:1mm 4mm;margin-top:1.4mm;font-size:var(--fs-micro);color:var(--ink-2)}
.legend span{display:inline-flex;align-items:center;gap:1.2mm}
.dot{display:inline-block;width:2.5mm;height:2.5mm;border-radius:50%}
.dot--navy{background:var(--navy)}.dot--maroon{background:var(--maroon);width:1.5mm;height:1.5mm;box-shadow:0 0 0 .55mm var(--paper),0 0 0 .9mm var(--maroon);margin:0 .5mm}
.trend-state{display:inline-flex;align-items:center;gap:1.2mm;font-weight:600;font-size:var(--fs-strong);color:var(--navy);margin-bottom:1.6mm}
.trend-state--up::before{content:"";width:0;height:0;border-left:1.3mm solid transparent;border-right:1.3mm solid transparent;border-bottom:2.2mm solid var(--maroon)}
.trend-state--flat::before{content:"";width:2.6mm;height:.6mm;background:var(--navy-2);border-radius:.3mm}
.plat li{display:grid;grid-template-columns:auto 1fr auto;grid-template-areas:"name state price" "roles roles roles";column-gap:2mm;row-gap:.9mm;align-items:center;padding:1.5mm 0;border-bottom:.25mm solid var(--line-soft)}
.plat li:first-child{padding-top:0}
.p-name{grid-area:name;font-weight:600;font-size:var(--fs-label)}
.p-state{grid-area:state;display:flex;align-items:center;gap:1.2mm;font-size:var(--fs-micro);color:var(--ink-2)}
.p-roles{grid-area:roles;display:flex;gap:1mm}
.p-roles:empty{display:none}
.p-price{grid-area:price;font-weight:600;font-size:var(--fs-label);color:var(--navy);font-variant-numeric:tabular-nums}
.qc{width:100%;border-collapse:collapse;font-size:var(--fs-micro)}
.qc th{font-weight:500;color:var(--ink-2);text-align:center;padding:0 .5mm 1.2mm;line-height:1.2}
.qc tbody th{text-align:left;font-weight:600;color:var(--ink);font-size:var(--fs-label);padding:1.1mm 0}
.qc td{text-align:center;padding:1.1mm 0;border-top:.25mm solid var(--line-soft)}
.qc tbody th{border-top:.25mm solid var(--line-soft)}
.qc td .glyph{margin:0 auto}
.legend--qc{gap:.6mm 3mm}
.pb-bottom{display:grid;grid-template-columns:2fr 1fr;gap:6mm;margin-top:1mm;break-inside:avoid}
.pb-bottom .prices{gap:6mm}
.sup-sec{margin-top:3.8mm;break-inside:avoid}
.prices-sec .sub+.price-list{margin-bottom:2mm}
.ex-list li{display:grid;grid-template-columns:1fr auto;column-gap:2mm;padding:1.4mm 0;border-top:.25mm solid var(--line-soft);font-size:var(--fs-label);line-height:1.35}
.ex-list .soft{grid-column:1/-1;font-size:var(--fs-micro)}
.ex-price{font-weight:600;color:var(--navy);font-variant-numeric:tabular-nums}
.prices{display:grid;grid-template-columns:1fr 1fr;gap:8mm}
.prices .sub{margin-bottom:.6mm}
.sub{font-family:var(--cond);font-weight:600;font-size:var(--fs-kicker);letter-spacing:.06em;text-transform:uppercase;color:var(--ink-2);margin-bottom:1.4mm}
.price-list li{display:grid;grid-template-columns:14mm 1fr;column-gap:2mm;padding:1.1mm 0;border-top:.25mm solid var(--line-soft);font-size:var(--fs-label);line-height:1.35}
.price-list .price{grid-row:span 2;font-size:var(--fs-module);color:var(--navy);line-height:1.15}
.price-list .soft{font-size:var(--fs-micro)}
.bulk{margin-top:2mm;padding:1.6mm 2.6mm;line-height:1.4;border-left:.8mm solid var(--maroon);background:var(--maroon-pale);font-size:var(--fs-label);color:var(--ink)}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:4mm}
.card{background:var(--card);border-radius:2mm;padding:3mm 3.4mm;border:.25mm solid var(--line-soft);break-inside:avoid}
.card-name{font-weight:600;font-size:var(--fs-strong);line-height:1.3}
.card .soft{font-size:var(--fs-micro);margin-top:.3mm}
.moq{display:flex;justify-content:space-between;align-items:baseline;gap:2mm;margin:2.2mm 0 1.4mm;padding:1.4mm 0;border-top:.25mm solid var(--line-soft);border-bottom:.25mm solid var(--line-soft);font-size:var(--fs-micro);color:var(--ink-2)}
.moq b{font-size:var(--fs-label);color:var(--navy)}
.terms li{display:grid;grid-template-columns:3.2mm 1fr auto;gap:1.6mm;align-items:center;font-size:var(--fs-micro);line-height:1.75}
.terms b{font-weight:600;color:var(--ink)}
.sup{width:100%;border-collapse:collapse;table-layout:fixed;font-size:var(--fs-micro)}
.sup thead th{font-family:var(--cond);font-weight:600;font-size:var(--fs-micro);letter-spacing:.04em;text-transform:uppercase;color:var(--ink-2);text-align:left;vertical-align:bottom;line-height:1.2;padding:0 1.6mm 1.4mm 0;border-bottom:.3mm solid var(--line)}
.sup thead th.s-name{font-family:var(--sans);font-size:var(--fs-strong);letter-spacing:0;text-transform:none;color:var(--navy);width:50mm}
.sup tbody th,.sup td{padding:1.4mm 1.6mm 1.4mm 0;border-bottom:.25mm solid var(--line-soft);text-align:left;vertical-align:top}
.sup .s-name b{display:block;font-size:var(--fs-label);color:var(--ink);line-height:1.3}
.sup .s-name span{display:block;font-weight:400;color:var(--ink-2);margin-top:.3mm}
.sup .s-moq b{font-size:var(--fs-label);color:var(--navy)}
.term{display:inline-flex;align-items:center;gap:1.1mm;color:var(--ink)}
.sup-note{font-size:var(--fs-micro);color:var(--ink-2);margin-top:2mm}
.examples{width:100%;border-collapse:collapse;font-size:var(--fs-micro);table-layout:fixed}
.examples th{font-family:var(--cond);font-weight:600;font-size:var(--fs-micro);letter-spacing:.06em;text-transform:uppercase;color:var(--ink-2);text-align:left;padding:0 2mm 1.2mm 0;border-bottom:.3mm solid var(--line)}
.examples td{padding:1.2mm 2mm 1.2mm 0;border-bottom:.25mm solid var(--line-soft);vertical-align:top}
.examples th:nth-child(1){width:30%}.examples th:nth-child(5),.examples th:nth-child(6){width:11%}
.examples td:first-child{font-weight:600}
.src-sec{margin-top:3.2mm;break-inside:avoid}
.src-sec .h-module{margin-bottom:1.2mm}
.sources{display:flex;flex-wrap:wrap;gap:.8mm 4mm;font-size:var(--fs-micro);color:var(--ink-2)}
.sources a{color:var(--maroon);border-bottom:.25mm solid rgba(107,31,43,.35)}

/* key page */
.key-grid{display:grid;grid-template-columns:1fr 1fr;gap:8mm}
.key-score-block{padding-bottom:1mm;margin-bottom:5mm;border-bottom:.3mm solid var(--line)}
.key-score{display:grid;grid-template-columns:30mm 1fr;gap:5mm;align-items:center;margin-bottom:3mm;max-width:150mm}
.key-parts{display:grid;grid-template-columns:repeat(4,1fr);gap:5mm}
.key-parts li{font-size:var(--fs-label);line-height:1.4;padding-top:1.6mm;border-top:.6mm solid var(--navy)}
.key-block{margin-bottom:4.2mm;break-inside:avoid}
.key-score p,.key-crowd p,.key-p,.key-comply p{font-size:var(--fs-label);line-height:1.45}
.key-parts b{color:var(--navy);font-size:var(--fs-strong)}
.key-parts .max{font-size:var(--fs-micro);color:var(--ink-2);margin-right:1mm}
.key-labels li{display:grid;grid-template-columns:37mm 1fr;gap:2mm;align-items:start;margin-bottom:2mm;font-size:var(--fs-label);line-height:1.4}
.key-labels .chip{justify-self:start}
.key-crowd{display:grid;grid-template-columns:28mm 1fr;gap:4mm;align-items:center}
.key-states li{display:grid;grid-template-columns:auto 1fr;gap:2.2mm;align-items:center;font-size:var(--fs-label);line-height:1.4;margin-bottom:1.6mm}
.key-states .est{justify-self:start}
.key-pips{display:flex;align-items:center;gap:2.4mm;margin-top:2mm;font-size:var(--fs-micro);color:var(--ink-2)}
.key-signals li{display:grid;grid-template-columns:17mm 1fr;gap:2.4mm;align-items:start;font-size:var(--fs-label);line-height:1.4;margin-bottom:2.2mm}
.sig{display:flex;gap:1.2mm;align-items:center;padding-top:.4mm}
.band{display:inline-block;width:4mm;height:3mm}
.band--sand{background:var(--paper-deep)}.band--rose{background:var(--maroon-pale)}
.key-p .glyph{display:inline-block;vertical-align:-.6mm}
.key-comply{display:grid;gap:1.6mm}
.key-comply .chip{justify-self:start}
.key-not-line{margin-top:1mm;padding:2.6mm 4mm;background:var(--paper-deep);border-radius:1.6mm;font-size:var(--fs-label);line-height:1.45}
.key-not{margin-top:1mm;padding:4mm 5mm;background:var(--paper-deep);border-radius:2mm;break-inside:avoid}
.key-not ul{display:grid;grid-template-columns:1fr 1fr;gap:1.6mm 8mm}
.key-not li,.notes-box li{position:relative;padding-left:3.6mm;font-size:var(--fs-label);line-height:1.45}
.key-not li::before,.notes-box li::before{content:"";position:absolute;left:0;top:1.7mm;width:1.4mm;height:1.4mm;border-radius:50%;box-shadow:inset 0 0 0 .3mm var(--maroon)}

/* closing */
.steps{display:grid;grid-template-columns:repeat(4,1fr);gap:5mm;margin:2mm 0 7mm;counter-reset:none}
.steps li{padding-top:3mm;border-top:.6mm solid var(--navy);font-size:var(--fs-label);line-height:1.45}
.step-n{display:inline-flex;align-items:center;justify-content:center;width:6mm;height:6mm;border-radius:50%;background:var(--maroon);color:#fff;font-weight:600;font-size:var(--fs-label);margin-bottom:2mm}
.step-t{font-family:var(--head);font-weight:600;font-size:var(--fs-module);color:var(--navy);margin-bottom:1mm}
.close-grid{display:grid;grid-template-columns:1.1fr 1fr;gap:8mm;margin-bottom:6mm}
.sites{display:flex;flex-wrap:wrap;gap:1.6mm}
.sites li{font-size:var(--fs-label);padding:.9mm 2.2mm;border-radius:1.2mm;background:var(--card);border:.25mm solid var(--line-soft)}
.issue-facts div{display:grid;grid-template-columns:30mm 1fr;gap:2mm;padding:1.3mm 0;border-top:.25mm solid var(--line-soft);font-size:var(--fs-label)}
.issue-facts dt{color:var(--ink-2)}
.issue-facts dd{font-weight:600}
.notes-box{padding:4.5mm 5mm;background:var(--paper-deep);border-radius:2mm}
.notes-box ul{display:grid;gap:1.8mm}
.sample-note{margin-top:5mm;font-size:var(--fs-micro);color:var(--maroon)}

/* phone (390 px class) */
@media screen and (max-width:640px){
  :root{--fs-micro:12px;--fs-kicker:12px;--fs-label:13px;--fs-strong:14px;--fs-note:14.5px;--fs-body:15px;--fs-lead:16px;
    --fs-module:17px;--fs-section:19px;--fs-stat:21px;--fs-title:23px;--fs-kpi:26px;--fs-display:30px}
  html{background:var(--paper)}body{padding:0}
  .sheet{width:auto;min-height:0;margin:0;padding:24px 16px 40px;box-shadow:none;border-bottom:8px solid #E2DBD3}
  .sheet.cover{height:auto;padding:0}
  .screen-foot{position:static;margin-top:20px;gap:12px;font-size:11px}
  .cover-inner{padding:20px 16px 0}.logo{width:120px}
  .cover-title{margin-top:28px}
  .cover-kpis,.kpis{grid-template-columns:1fr 1fr}
  .cover-kpis div:nth-child(3),.kpi:nth-child(3){padding-left:0;border-left:0}
  .cover-kpis div:nth-child(n+3),.kpi:nth-child(n+3){border-top:1px solid var(--line-soft)}
  .mosaic{grid-template-columns:1fr 1fr;gap:18px 12px}
  .mosaic .plate{height:150px}
  .cover-foot{flex-direction:column;align-items:flex-start;gap:10px;padding:20px 16px 24px;margin-top:28px}
  .mixes{grid-template-columns:1fr;gap:18px}
  .stack{width:100%;height:14px}
  .cat-list li{grid-template-columns:140px 1fr auto}
  .league thead{display:none}
  .league,.league tbody{display:block}
  .league tr{display:grid;grid-template-columns:30px 1fr auto;grid-template-areas:"rk pt pt" "rk sc ft" "rk lb cr";gap:6px 10px;padding:12px 0;border-bottom:1px solid var(--line-soft)}
  .league td{display:block;padding:0;border:0;width:auto!important}
  .league .c0{grid-area:rk}.league .c1{grid-area:pt}.league .c2{grid-area:sc}.league .c3{grid-area:lb}.league .c4{grid-area:cr;justify-self:end}.league .c5{grid-area:ft;text-align:right}
  .league tr.group{display:block;padding:16px 0 4px}
  .league tr.group td{padding:0;border:0}
  .cbar{width:150px;height:9px}
  .pa-head{grid-template-columns:1fr;row-gap:18px}
  .pa-photo .plate--big{height:auto;aspect-ratio:1/1}
  .pa-photo{margin-bottom:12px}
  .pa-dial{flex-direction:row;justify-content:flex-start;gap:14px}
  .pa-dial .dial{width:120px;height:120px}
  .label-line{max-width:none}
  .parts{grid-template-columns:1fr 1fr;gap:18px 16px}
  .partbar{width:100%;height:8px}
  .notes:has(.note--update){grid-template-columns:1fr}
  .fit-head{flex-direction:column;align-items:flex-start;gap:8px}
  .fit-list{columns:1}
  .fitpips{width:150px;height:auto}
  .pb-strip{flex-wrap:wrap}
  .sec-head{flex-direction:column;gap:4px}
  .tiles--2,.tiles--3,.prices,.cards,.key-grid,.close-grid,.steps,.key-not ul,.pb-bottom,.pb-bottom .prices{grid-template-columns:1fr}
  .pb-bottom{gap:16px}
  .key-labels li{grid-template-columns:1fr;gap:4px}
  .timeline{width:100%;height:auto}
  .spark{width:100%;max-width:300px;height:auto}
  .gauge{width:130px;height:auto}
  .examples,.examples thead,.examples tbody,.examples tr,.examples td{display:block}
  .examples thead{display:none}
  .examples tr{padding:8px 0;border-bottom:1px solid var(--line-soft)}
  .examples td{border:0;padding:1px 0}
  .key-score{grid-template-columns:110px 1fr}.key-score .dial{width:110px;height:110px}
  .key-crowd{grid-template-columns:110px 1fr}.key-crowd .gauge{width:110px}
  .price-list li{grid-template-columns:72px 1fr}
  .key-parts{grid-template-columns:1fr 1fr;gap:14px}
  .sup,.sup tbody,.sup tr,.sup th,.sup td{display:block;width:auto!important}
  .sup thead{display:none}
  .sup tr{padding:12px 14px;margin-bottom:10px;background:var(--card);border-radius:8px;box-shadow:0 0 0 1px var(--line-soft)}
  .sup tbody th,.sup td{border:0;padding:3px 0}
  .sup td{display:flex;justify-content:space-between;gap:12px}
  .sup td::before{content:attr(data-term);color:var(--ink-2)}
  .pb-strip{flex-wrap:wrap;row-gap:6px}
  .pb-title{flex:1 1 calc(100% - 50px)}
  .pb-sec{display:block;margin:4px 0 0}
}
"""


def page_css(t):
    foot = t["foot"].format(date=long_date(ISSUE["date"]))
    page_word = "Page " if t["of"] == "of" else "Page "
    return (CSS.replace('content:var(--foot-text, "NeuraFlow · Product Opportunity Brief")', f'content:"{foot}"')
               .replace('content:"Page " counter(page) " of " counter(pages)',
                        f'content:"{page_word}" counter(page) " {t["of"]} " counter(pages)'))


def document(lang, pages):
    t = COPY[lang]
    title = "NeuraFlow report redesign mock-up (sample data, " + ("English" if lang == "en" else "Roman Hinglish") + ")"
    return ('<!doctype html><html lang="' + ("en" if lang == "en" else "hi-Latn") + '"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><title>' + escape(title) + '</title>'
            '<style>' + font_css() + page_css(t) + '</style></head><body>' + "\n".join(pages) + '</body></html>')


def build():
    rows = products()
    by_key = {r["key"]: r for r in rows}
    en = COPY["en"]
    pages_en = [cover(rows, en, "en"), glance(rows, en, "en"), key_page(en)]
    for key in ("plant", "feeding", "chopper"):
        pages_en += [product_a(by_key[key], SPREADS[key], en, "en"), product_b(by_key[key], SPREADS[key], en, "en")]
    pages_en.append(closing(en))
    hi = COPY["hi-Latn"]
    pages_hi = [cover(rows, hi, "hi-Latn"), glance(rows, hi, "hi-Latn"),
                product_a(by_key["feeding"], SPREADS["feeding"], hi, "hi-Latn"),
                product_b(by_key["feeding"], SPREADS["feeding"], hi, "hi-Latn")]
    for lang, pages in (("en", pages_en), ("hi-Latn", pages_hi)):
        html = document(lang, pages)
        assert "<script" not in html.lower() and "<iframe" not in html.lower()
        assert not re.search(r'(?:src|href)=["\']https?:|url\(["\']?https?:', html), "external resource found"
        path = OUT / f"mockup-{lang}.html"
        path.write_text(html, encoding="utf-8")
        print(path.name, len(html) // 1024, "KB,", len(pages), "sheets")


if __name__ == "__main__":
    build()
