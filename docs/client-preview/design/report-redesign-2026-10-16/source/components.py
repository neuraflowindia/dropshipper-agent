"""Small drawing helpers for the report redesign mock-up.

Every chart is inline SVG measured in millimetres (viewBox units = mm), so the
A4 print is exact and the phone view can scale it with CSS. No script, no
network. In production each function here would become one small renderer
fed by code-owned facts (see plan.md, section 12).
"""

from datetime import date
from html import escape
import math

PT = 25.4 / 72  # millimetres per point

INK, INK2 = "#292C2F", "#5F6062"
NAVY, NAVY2, TRACK = "#24384B", "#647487", "#DCE3EA"
MAROON, MAROON_PALE = "#6B1F2B", "#F1DFE2"
PAPER, PAPER_DEEP, LINE = "#F9F3EC", "#EEE5DB", "#D8D0C8"
GREY = "#8E8F91"
LABEL = {"sweet_spot": "#2A6CA3", "early_bet": "#B07A12", "proven_but_crowded": "#9A2F3E"}
CROWD = {"low": "#C99AA2", "medium": "#9C4E5B", "high": "#6B1F2B"}
CROWD_TRACK = "#E9E0D7"


def fmt_in(n):
    """Indian digit grouping: 38412 -> 38,412; 1240000 -> 12,40,000."""
    s = str(int(n))
    if len(s) <= 3:
        return s
    head, tail, groups = s[:-3], s[-3:], []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


def svg(w, h, body, cls="", label=""):
    aria = f' role="img" aria-label="{escape(label, quote=True)}"' if label else ' aria-hidden="true"'
    return (f'<svg class="{cls}" width="{w:.2f}mm" height="{h:.2f}mm" viewBox="0 0 {w:.2f} {h:.2f}"'
            f' xmlns="http://www.w3.org/2000/svg"{aria}>{body}</svg>')


def text(x, y, value, size_pt, *, anchor="start", weight=400, fill=INK2, family="sans"):
    fam = {"sans": "NF Sans, Segoe UI, Arial, sans-serif", "cond": "NF Sans Cond, Arial Narrow, sans-serif"}[family]
    return (f'<text x="{x:.2f}" y="{y:.2f}" font-family="{fam}" font-size="{size_pt * PT:.2f}" font-weight="{weight}"'
            f' fill="{fill}" text-anchor="{anchor}">{escape(str(value))}</text>')


# --- Glyphs -------------------------------------------------------------------------------------

def glyph_label(label, size=3.0):
    c = LABEL[label]
    shapes = {
        "sweet_spot": f'<circle cx="5" cy="5" r="3.7" fill="none" stroke="{c}" stroke-width="1.5"/><circle cx="5" cy="5" r="1.6" fill="{c}"/>',
        "early_bet": f'<path d="M5 .9 9.2 5.9H6.5V9.1H3.5V5.9H.8Z" fill="{c}"/>',
        "proven_but_crowded": f'<rect x=".9" y="1.2" width="8.2" height="1.7" rx=".85" fill="{c}"/><rect x=".9" y="4.15" width="8.2" height="1.7" rx=".85" fill="{c}"/><rect x=".9" y="7.1" width="8.2" height="1.7" rx=".85" fill="{c}"/>',
    }
    return f'<svg class="glyph" width="{size}mm" height="{size}mm" viewBox="0 0 10 10" aria-hidden="true">{shapes[label]}</svg>'


def glyph_state(state, size=3.6):
    """Checklist / supplier-term / platform state: shape carries meaning (greyscale-safe)."""
    if state in ("Meets", "yes", "listed", "instock"):
        body = (f'<circle cx="5" cy="5" r="4.6" fill="{NAVY}"/>'
                f'<path d="M2.8 5.2 4.3 6.7 7.3 3.5" fill="none" stroke="#fff" stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round"/>')
    elif state in ("Does not meet", "no", "not_found", "nostock"):
        body = (f'<circle cx="5" cy="5" r="4.1" fill="none" stroke="{GREY}" stroke-width="1"/>'
                f'<path d="M3.5 3.5 6.5 6.5M6.5 3.5 3.5 6.5" stroke="{GREY}" stroke-width="1" stroke-linecap="round"/>')
    elif state == "Unclear":
        body = (f'<circle cx="5" cy="5" r="4.1" fill="none" stroke="{NAVY2}" stroke-width="1"/>'
                f'<path d="M5 .9A4.1 4.1 0 0 0 5 9.1Z" fill="{NAVY2}"/>')
    else:  # unknown / not stated / not checked
        body = (f'<circle cx="5" cy="5" r="4.1" fill="none" stroke="{GREY}" stroke-width="1" stroke-dasharray="1.6 1.1"/>'
                f'<path d="M3.4 5H6.6" stroke="{GREY}" stroke-width="1" stroke-linecap="round"/>')
    return f'<svg class="glyph" width="{size}mm" height="{size}mm" viewBox="0 0 10 10" aria-hidden="true">{body}</svg>'


ICONS = {  # 24-unit line icons, drawn with currentColor
    "Home and Kitchen": "M4 10h16v5.5A4.5 4.5 0 0 1 15.5 20h-7A4.5 4.5 0 0 1 4 15.5zM2 10h20M9.5 7.2c0-1.2 1.2-1.6 1.2-2.8M13.5 7.2c0-1.2 1.2-1.6 1.2-2.8",
    "Home and Decor": "M8.5 3h7l3.5 8H5zM12 11v8M8 21h8",
    "Baby Products": "M10 2.5h4v2.5h-4zM9 5h6l1 3v11a2 2 0 0 1-2 2h-4a2 2 0 0 1-2-2V8zM8.5 12h3M8.5 15h3",
    "Travel and Outdoor": "M4.5 8h15v11.5a1 1 0 0 1-1 1h-13a1 1 0 0 1-1-1zM9 8V4.5h6V8M9 11.5v6M15 11.5v6",
    "shield": "M12 2.5l7.5 2.8v5.6c0 4.8-3.2 8.6-7.5 10.6-4.3-2-7.5-5.8-7.5-10.6V5.3zM12 7.5v5.5M12 16.2v.3",
    "photo": "M3.5 6.5h17v12h-17zM3.5 15.5l5-4.5 4 3.5 3-2.5 5 4M15.5 10a1.2 1.2 0 1 0 0-.01",
}


def icon(name, size=4.0, stroke=1.5, cls="icon"):
    return (f'<svg class="{cls}" width="{size}mm" height="{size}mm" viewBox="0 0 24 24" aria-hidden="true">'
            f'<path d="{ICONS[name]}" fill="none" stroke="currentColor" stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round"/></svg>')


# --- Score -------------------------------------------------------------------------------------

def _arc(cx, cy, r, a0, a1):
    x0, y0 = cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0))
    x1, y1 = cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1))
    large = 1 if (a1 - a0) > 180 else 0
    return f"M{x0:.3f} {y0:.3f}A{r} {r} 0 {large} 1 {x1:.3f} {y1:.3f}"


def score_dial(parts, maxima, total, *, size=36.0, caption="out of 100", label=""):
    """A ring meter split into four compartments sized by each part's maximum (20/30/25/25),
    clockwise from the top in the order Want, Early, Room, Product. One hue: points filled
    in navy, the unearned remainder in the pale track."""
    cx = cy = size / 2
    sw = 3.3
    r = size / 2 - sw / 2 - .4
    gap = 4.0
    usable = 360 - gap * 4
    cursor = -90 + gap / 2
    body = []
    for part in ("want", "early", "room", "product"):
        span = usable * maxima[part] / 100
        body.append(f'<path d="{_arc(cx, cy, r, cursor, cursor + span)}" fill="none" stroke="{TRACK}" stroke-width="{sw}"/>')
        if parts[part]:
            filled = span * parts[part] / maxima[part]
            body.append(f'<path d="{_arc(cx, cy, r, cursor, cursor + filled)}" fill="none" stroke="{NAVY}" stroke-width="{sw}"/>')
        cursor += span + gap
    big = 26 if size >= 34 else 22
    body.append(text(cx, cy + big * PT * .36, total, big, anchor="middle", weight=600, fill=NAVY))
    body.append(text(cx, cy + big * PT * .36 + 4.2, caption, 8, anchor="middle", fill=INK2))
    return svg(size, size, "".join(body), "dial", label)


def part_bar(points, maximum, *, scale=2.6, h=2.4):
    """One part of the score on a shared scale (1 point = `scale` mm), track = the part's maximum."""
    w = maximum * scale
    fill = points * scale
    body = f'<rect x="0" y="0" width="{w:.2f}" height="{h}" rx="{h / 2}" fill="{TRACK}"/>'
    if points:
        body += f'<rect x="0" y="0" width="{max(fill, h):.2f}" height="{h}" rx="{h / 2}" fill="{NAVY}"/>'
    return svg(w, h, body, "partbar")


def compartment_bar(parts, maxima, *, w=30.0, h=2.6, label=""):
    """Compact 0-100 bar for the league table: four compartments, each filled by its points."""
    k = w / 100
    body, start = [], 0
    for part in ("want", "early", "room", "product"):
        x0 = start * k + .25
        cw = maxima[part] * k - .5
        body.append(f'<rect x="{x0:.2f}" y="0" width="{cw:.2f}" height="{h}" fill="{TRACK}"/>')
        if parts[part]:
            body.append(f'<rect x="{x0:.2f}" y="0" width="{cw * parts[part] / maxima[part]:.2f}" height="{h}" fill="{NAVY}"/>')
        start += maxima[part]
    return svg(w, h, "".join(body), "cbar", label)


# --- Crowding ----------------------------------------------------------------------------------

LEVELS = ("low", "medium", "high")


def crowd_gauge(level, *, w=34.0, low="Low", high="High"):
    """Half-ring gauge with three ordinal segments (one hue, light to dark) and a needle."""
    cx, cy, r, sw = w / 2, w / 2 - 1.2, w / 2 - 3.2, 4.0
    h = cy + 5.6
    gap = 2.0
    idx = LEVELS.index(level)
    body = []
    for i, name in enumerate(LEVELS):
        a0 = 180 + i * 60 + gap / 2
        a1 = 180 + (i + 1) * 60 - gap / 2
        colour = CROWD[name] if i <= idx else CROWD_TRACK
        body.append(f'<path d="{_arc(cx, cy, r, a0, a1)}" fill="none" stroke="{colour}" stroke-width="{sw}"/>')
    ang = math.radians(180 + idx * 60 + 30)
    nx, ny = cx + (r - 3.4) * math.cos(ang), cy + (r - 3.4) * math.sin(ang)
    body.append(f'<path d="M{cx:.2f} {cy:.2f}L{nx:.2f} {ny:.2f}" stroke="{INK}" stroke-width=".6" stroke-linecap="round"/>')
    body.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="1.3" fill="{INK}" stroke="{PAPER}" stroke-width=".4"/>')
    body.append(text(cx - r, cy + 4.8, low, 8, anchor="middle"))
    body.append(text(cx + r, cy + 4.8, high, 8, anchor="middle"))
    return svg(w, h, "".join(body), "gauge", "Crowding " + level)


def crowd_pips(level, *, size=2.3):
    """Three small squares filled up to the level (league table)."""
    idx = LEVELS.index(level)
    body = "".join(f'<rect x="{i * (size + .6):.2f}" y="0" width="{size}" height="{size}" rx=".35" fill="{CROWD[n] if i <= idx else CROWD_TRACK}"/>'
                   for i, n in enumerate(LEVELS))
    return svg(3 * size + 1.2, size, body, "pips")


def fit_pips(states, *, d=2.6, gap=.9):
    """Ten dots in checklist order: filled = Meets, half = Unclear, ring = Does not meet."""
    body = []
    for i, state in enumerate(states):
        cx, cy, r = i * (d + gap) + d / 2, d / 2, d / 2 - .25
        if state == "Meets":
            body.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="{NAVY}"/>')
        elif state == "Unclear":
            body.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="none" stroke="{NAVY2}" stroke-width=".3"/>'
                        f'<path d="M{cx:.2f} {cy - r:.2f}A{r:.2f} {r:.2f} 0 0 0 {cx:.2f} {cy + r:.2f}Z" fill="{NAVY2}"/>')
        else:
            body.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r - .1:.2f}" fill="none" stroke="{GREY}" stroke-width=".3"/>')
    return svg(len(states) * (d + gap) - gap, d, "".join(body), "fitpips")


# --- Market signals ----------------------------------------------------------------------------

def _d(value):
    return date.fromisoformat(value)


def meta_timeline(starts, observed, *, w=76.0, months=None):
    """Each dot = one advertiser, placed at the start date of its earliest relevant ad, on the
    12 months before the observation. Dots inside the last 30 days are maroon (emphasis form)."""
    h = 15.0
    end = _d(observed)
    begin = date(end.year - 1, end.month, end.day)
    span = (end - begin).days
    x0, x1, axis = 1.6, w - 1.6, 10.2

    def xpos(day):
        return x0 + (x1 - x0) * (day - begin).days / span

    band = xpos(date.fromordinal(end.toordinal() - 30))
    body = [f'<rect x="{band:.2f}" y=".4" width="{x1 - band:.2f}" height="{axis - .4:.2f}" fill="{MAROON_PALE}"/>',
            f'<path d="M{x0} {axis}H{x1}" stroke="{LINE}" stroke-width=".3"/>']
    m = date(begin.year, begin.month, 1)
    labels = months or {}
    while m <= end:
        if m >= begin:
            x = xpos(m)
            body.append(f'<path d="M{x:.2f} {axis}v1" stroke="{LINE}" stroke-width=".3"/>')
            if m.month in (1, 4, 7, 10):
                name = labels.get(m.month, m.strftime("%b"))
                if m.month == 1:
                    name += " " + str(m.year)
                body.append(text(x, axis + 4.4, name, 8, anchor="middle"))
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    placed = []
    for value in sorted(starts):
        x = xpos(_d(value))
        row = 0
        while any(abs(x - px) < 2.7 and pr == row for px, pr in placed):
            row += 1
        placed.append((x, row))
        recent = (end - _d(value)).days <= 30
        cy = axis - 3.0 - row * 2.9
        if recent:  # shape, not only colour: a ringed dot survives greyscale printing
            body.append(f'<circle cx="{x:.2f}" cy="{cy:.2f}" r="1.75" fill="{PAPER}" stroke="{MAROON}" stroke-width=".4"/>'
                        f'<circle cx="{x:.2f}" cy="{cy:.2f}" r=".9" fill="{MAROON}"/>')
        else:
            body.append(f'<circle cx="{x:.2f}" cy="{cy:.2f}" r="1.25" fill="{NAVY}" stroke="{PAPER}" stroke-width=".45"/>')
    return svg(w, h, "".join(body), "timeline", f"{len(starts)} advertisers by first ad start date")


def trends_spark(series, *, w=48.0, left="Oct 2025", right="Oct 2026"):
    """Weekly Google Trends interest (0-100, relative) for 12 months. The two four-week windows the
    score compares are shaded: previous (sand) and recent (rose)."""
    h = 18.5
    top, bottom, x0, x1 = 1.0, 13.0, .8, w - 1.6
    n = len(series)
    step = (x1 - x0) / (n - 1)

    def pt(i, v):
        return x0 + i * step, bottom - (bottom - top) * v / 100

    prev_a, rec_a = pt(n - 8, 0)[0] - step / 2, pt(n - 4, 0)[0] - step / 2
    body = [f'<rect x="{prev_a:.2f}" y="{top:.2f}" width="{4 * step:.2f}" height="{bottom - top:.2f}" fill="{PAPER_DEEP}"/>',
            f'<rect x="{rec_a:.2f}" y="{top:.2f}" width="{x1 - rec_a + .8:.2f}" height="{bottom - top:.2f}" fill="{MAROON_PALE}"/>',
            f'<path d="M{x0} {bottom}H{x1 + .8:.2f}" stroke="{LINE}" stroke-width=".3"/>']
    points = [pt(i, v) for i, v in enumerate(series)]
    line = "M" + "L".join(f"{x:.2f} {y:.2f}" for x, y in points)
    body.append(f'<path d="{line}L{points[-1][0]:.2f} {bottom}L{x0} {bottom}Z" fill="{NAVY}" fill-opacity=".10"/>')
    body.append(f'<path d="{line}" fill="none" stroke="{NAVY}" stroke-width=".55" stroke-linejoin="round" stroke-linecap="round"/>')
    ex, ey = points[-1]
    body.append(f'<circle cx="{ex:.2f}" cy="{ey:.2f}" r="1.1" fill="{MAROON}" stroke="{PAPER}" stroke-width=".45"/>')
    body.append(text(x0, bottom + 4.5, left, 8))
    body.append(text(x1 + .8, bottom + 4.5, right, 8, anchor="end"))
    return svg(w, h, "".join(body), "spark", "Google Trends India, weekly relative interest")


def qc_matrix(cities, rows, *, labels):
    """Apps x cities. Filled = in stock, cross ring = not in stock / not listed, dashed ring = not checked."""
    head = "".join(f'<th scope="col">{escape(c)}</th>' for c in cities)
    body = ""
    for app, states in rows.items():
        cells = "".join(f'<td>{glyph_state({"yes": "instock", "no": "nostock"}.get(s, "unknown"), 3.2)}'
                        f'<span class="sr">{escape(labels[s])}</span></td>' for s in states)
        body += f'<tr><th scope="row">{escape(app)}</th>{cells}</tr>'
    return f'<table class="qc"><thead><tr><th></th>{head}</tr></thead><tbody>{body}</tbody></table>'
