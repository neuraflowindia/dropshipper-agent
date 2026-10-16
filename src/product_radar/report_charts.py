"""Pure inline-SVG drawings for the client report (no script, no network).

Every chart is measured in millimetres (viewBox units = mm), so the A4 print
is exact and the phone view can scale it with CSS. Each function takes
already-checked, code-owned facts and returns markup; nothing here reads
research records or decides a fact. Meaning never rides on colour alone:
labels have distinct glyph shapes, states have distinct symbols, crowding is
a lightness ramp plus a needle plus a word, recent advertisers are a ringed
dot. Ported from the approved mock-up (report redesign, 16 October 2026).
"""

from datetime import date, timedelta
from html import escape
import math

from .report_theme import COLOUR, CROWD, CROWD_TRACK, LABEL

PT = 25.4 / 72  # millimetres per point
LEVELS = ("low", "medium", "high")
PARTS = ("want", "early", "room", "product")
LABELS = ("sweet_spot", "early_bet", "proven_but_crowded")
# Gaps (degrees) between the score ring's four compartments.
RING_GAP = 4.0


def fmt_in(value):
    """Indian digit grouping: 38412 -> 38,412; 1240000 -> 12,40,000."""
    if type(value) is not int or value < 0:
        raise ValueError("fmt_in expects a non-negative int")
    digits = str(value)
    if len(digits) <= 3:
        return digits
    head, tail, groups = digits[:-3], digits[-3:], []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    groups.insert(0, head)
    return ",".join(groups) + "," + tail


def svg(width, height, body, cls="", label=""):
    aria = f' role="img" aria-label="{escape(label, quote=True)}"' if label else ' aria-hidden="true"'
    return (f'<svg class="{cls}" width="{width:.2f}mm" height="{height:.2f}mm" viewBox="0 0 {width:.2f} {height:.2f}"'
            f' xmlns="http://www.w3.org/2000/svg"{aria}>{body}</svg>')


def text(x, y, value, size_pt, *, anchor="start", weight=400, fill=None, family="sans"):
    """Real (extractable) text inside a drawing; never smaller than 8 pt."""
    if size_pt < 8:
        raise ValueError("report text is never smaller than 8 pt")
    fam = {"sans": "NF Sans, Segoe UI, Arial, sans-serif", "cond": "NF Sans Cond, Arial Narrow, sans-serif"}[family]
    return (f'<text x="{x:.2f}" y="{y:.2f}" font-family="{fam}" font-size="{size_pt * PT:.2f}" font-weight="{weight}"'
            f' fill="{fill or COLOUR["ink-2"]}" text-anchor="{anchor}">{escape(str(value))}</text>')


# Glyphs -------------------------------------------------------------------------

def glyph_label(label, size=3.0):
    colour = LABEL[label]
    shapes = {
        "sweet_spot": (f'<circle cx="5" cy="5" r="3.7" fill="none" stroke="{colour}" stroke-width="1.5"/>'
                       f'<circle cx="5" cy="5" r="1.6" fill="{colour}"/>'),
        "early_bet": f'<path d="M5 .9 9.2 5.9H6.5V9.1H3.5V5.9H.8Z" fill="{colour}"/>',
        "proven_but_crowded": "".join(f'<rect x=".9" y="{y}" width="8.2" height="1.7" rx=".85" fill="{colour}"/>'
                                      for y in ("1.2", "4.15", "7.1")),
    }
    return (f'<svg class="glyph" width="{size}mm" height="{size}mm" viewBox="0 0 10 10" aria-hidden="true">'
            f'{shapes[label]}</svg>')


# A state's symbol: filled tick (yes), crossed ring (no), half ring (unclear),
# dashed ring (not stated / not checked), dashed ring with two bars (conflicting).
STATE_SHAPES = {"yes": "yes", "Meets": "yes", "listed": "yes", "in_stock": "yes",
                "no": "no", "Does not meet": "no", "not_found": "no", "not_in_stock": "no", "not_listed": "no",
                "Unclear": "half",
                "unknown": "dashed", "not_stated": "dashed", "not_checked": "dashed",
                "conflicting": "conflict"}


def glyph_state(state, size=3.6):
    shape = STATE_SHAPES[state]
    navy, navy2, grey = COLOUR["navy"], COLOUR["navy-2"], COLOUR["grey"]
    if shape == "yes":
        body = (f'<circle cx="5" cy="5" r="4.6" fill="{navy}"/>'
                '<path d="M2.8 5.2 4.3 6.7 7.3 3.5" fill="none" stroke="#fff" stroke-width="1.25"'
                ' stroke-linecap="round" stroke-linejoin="round"/>')
    elif shape == "no":
        body = (f'<circle cx="5" cy="5" r="4.1" fill="none" stroke="{grey}" stroke-width="1"/>'
                f'<path d="M3.5 3.5 6.5 6.5M6.5 3.5 3.5 6.5" stroke="{grey}" stroke-width="1" stroke-linecap="round"/>')
    elif shape == "half":
        body = (f'<circle cx="5" cy="5" r="4.1" fill="none" stroke="{navy2}" stroke-width="1"/>'
                f'<path d="M5 .9A4.1 4.1 0 0 0 5 9.1Z" fill="{navy2}"/>')
    elif shape == "conflict":
        body = (f'<circle cx="5" cy="5" r="4.1" fill="none" stroke="{grey}" stroke-width="1" stroke-dasharray="1.6 1.1"/>'
                f'<path d="M3.6 4.1H6.4M3.6 5.9H6.4" stroke="{grey}" stroke-width="1" stroke-linecap="round"/>')
    else:
        body = (f'<circle cx="5" cy="5" r="4.1" fill="none" stroke="{grey}" stroke-width="1" stroke-dasharray="1.6 1.1"/>'
                f'<path d="M3.4 5H6.6" stroke="{grey}" stroke-width="1" stroke-linecap="round"/>')
    return (f'<svg class="glyph" width="{size}mm" height="{size}mm" viewBox="0 0 10 10" aria-hidden="true">'
            f'{body}</svg>')


ICONS = {  # 24-unit line icons drawn with currentColor
    "Home and Kitchen": ("M4 10h16v5.5A4.5 4.5 0 0 1 15.5 20h-7A4.5 4.5 0 0 1 4 15.5zM2 10h20M9.5 7.2c0-1.2 1.2-1.6"
                         " 1.2-2.8M13.5 7.2c0-1.2 1.2-1.6 1.2-2.8"),
    "Home and Decor": "M8.5 3h7l3.5 8H5zM12 11v8M8 21h8",
    "Baby Products": "M10 2.5h4v2.5h-4zM9 5h6l1 3v11a2 2 0 0 1-2 2h-4a2 2 0 0 1-2-2V8zM8.5 12h3M8.5 15h3",
    "Travel and Outdoor": "M4.5 8h15v11.5a1 1 0 0 1-1 1h-13a1 1 0 0 1-1-1zM9 8V4.5h6V8M9 11.5v6M15 11.5v6",
    "shield": "M12 2.5l7.5 2.8v5.6c0 4.8-3.2 8.6-7.5 10.6-4.3-2-7.5-5.8-7.5-10.6V5.3zM12 7.5v5.5M12 16.2v.3",
    "photo": "M3.5 6.5h17v12h-17zM3.5 15.5l5-4.5 4 3.5 3-2.5 5 4M15.5 10a1.2 1.2 0 1 0 0-.01",
}


def icon(name, size=4.0, stroke=1.5, cls="icon"):
    return (f'<svg class="{cls}" width="{size}mm" height="{size}mm" viewBox="0 0 24 24" aria-hidden="true">'
            f'<path d="{ICONS.get(name, ICONS["photo"])}" fill="none" stroke="currentColor" stroke-width="{stroke}"'
            ' stroke-linecap="round" stroke-linejoin="round"/></svg>')


# Score ---------------------------------------------------------------------------

def _arc(cx, cy, r, a0, a1):
    x0, y0 = cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0))
    x1, y1 = cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1))
    large = 1 if (a1 - a0) > 180 else 0
    return f"M{x0:.3f} {y0:.3f}A{r:.3f} {r:.3f} 0 {large} 1 {x1:.3f} {y1:.3f}"


def ring_spans(maxima):
    """Degrees per compartment, proportional to each part's maximum, after the four gaps."""
    total = sum(maxima[part] for part in PARTS)
    usable = 360 - RING_GAP * len(PARTS)
    return {part: usable * maxima[part] / total for part in PARTS}


def _check_parts(points, maxima):
    for part in PARTS:
        if (type(points.get(part)) is not int or type(maxima.get(part)) is not int or maxima[part] <= 0
                or not 0 <= points[part] <= maxima[part]):
            raise ValueError("score parts must be whole points within each part's maximum")


def score_ring(points, maxima, total, *, size=30.0, caption="out of 100", label=""):
    """The score out of 100 as a ring split into four compartments (Want, Early,
    Room, Product) clockwise from 12 o'clock; earned points navy, the rest track."""
    _check_parts(points, maxima)
    cx = cy = size / 2
    stroke = 3.3
    radius = size / 2 - stroke / 2 - .4
    cursor = -90 + RING_GAP / 2
    body = []
    for part, span in ring_spans(maxima).items():
        body.append(f'<path d="{_arc(cx, cy, radius, cursor, cursor + span)}" fill="none" stroke="{COLOUR["track"]}"'
                    f' stroke-width="{stroke}"/>')
        if points[part]:
            filled = span * points[part] / maxima[part]
            body.append(f'<path d="{_arc(cx, cy, radius, cursor, cursor + filled)}" fill="none" stroke="{COLOUR["navy"]}"'
                        f' stroke-width="{stroke}"/>')
        cursor += span + RING_GAP
    big = 22
    body.append(text(cx, cy + big * PT * .36, total, big, anchor="middle", weight=600, fill=COLOUR["navy"]))
    body.append(text(cx, cy + big * PT * .36 + 4.2, caption, 8, anchor="middle"))
    return svg(size, size, "".join(body), "dial", label)


def part_meter(points, maximum, *, width=38.0, height=2.2):
    """One part of the score: the track is the part's maximum, navy the points."""
    if type(points) is not int or type(maximum) is not int or maximum <= 0 or not 0 <= points <= maximum:
        raise ValueError("part points must be within the part's maximum")
    body = f'<rect x="0" y="0" width="{width:.2f}" height="{height}" rx="{height / 2}" fill="{COLOUR["track"]}"/>'
    if points:
        body += (f'<rect x="0" y="0" width="{max(width * points / maximum, height):.2f}" height="{height}"'
                 f' rx="{height / 2}" fill="{COLOUR["navy"]}"/>')
    return svg(width, height, body, "partbar")


def compartment_bar(points, maxima, *, width=30.0, height=2.6, label=""):
    """Compact 0-100 bar for the league table: four compartments, each filled by its points."""
    _check_parts(points, maxima)
    scale = width / sum(maxima[part] for part in PARTS)
    body, start = [], 0
    for part in PARTS:
        x0 = start * scale + .25
        cell = maxima[part] * scale - .5
        body.append(f'<rect x="{x0:.2f}" y="0" width="{cell:.2f}" height="{height}" fill="{COLOUR["track"]}"/>')
        if points[part]:
            body.append(f'<rect x="{x0:.2f}" y="0" width="{cell * points[part] / maxima[part]:.2f}" height="{height}"'
                        f' fill="{COLOUR["navy"]}"/>')
        start += maxima[part]
    return svg(width, height, "".join(body), "cbar", label)


# Crowding --------------------------------------------------------------------------

def crowd_gauge(level, *, width=30.0, low="Low", high="High", label=""):
    """Half ring with three ordinal segments (light to dark), filled up to the level, and a needle."""
    index = LEVELS.index(level)
    cx, cy, radius, stroke = width / 2, width / 2 - 1.2, width / 2 - 3.2, 4.0
    height = cy + 5.6
    gap = 2.0
    body = []
    for number, name in enumerate(LEVELS):
        a0, a1 = 180 + number * 60 + gap / 2, 180 + (number + 1) * 60 - gap / 2
        colour = CROWD[name] if number <= index else CROWD_TRACK
        body.append(f'<path d="{_arc(cx, cy, radius, a0, a1)}" fill="none" stroke="{colour}" stroke-width="{stroke}"/>')
    angle = math.radians(180 + index * 60 + 30)
    nx, ny = cx + (radius - 3.4) * math.cos(angle), cy + (radius - 3.4) * math.sin(angle)
    body.append(f'<path d="M{cx:.2f} {cy:.2f}L{nx:.2f} {ny:.2f}" stroke="{COLOUR["ink"]}" stroke-width=".6"'
                ' stroke-linecap="round"/>')
    body.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="1.3" fill="{COLOUR["ink"]}" stroke="{COLOUR["paper"]}"'
                ' stroke-width=".4"/>')
    body.append(text(cx - radius, cy + 4.8, low, 8, anchor="middle"))
    body.append(text(cx + radius, cy + 4.8, high, 8, anchor="middle"))
    return svg(width, height, "".join(body), "gauge", label or "Crowding " + level)


def needle_angle(level):
    """Degrees (SVG, clockwise from 3 o'clock) at which the needle points for a level."""
    return 180 + LEVELS.index(level) * 60 + 30


def crowd_pips(level, *, size=2.3):
    """Three small squares filled up to the level (league table)."""
    index = LEVELS.index(level)
    body = "".join(f'<rect x="{n * (size + .6):.2f}" y="0" width="{size}" height="{size}" rx=".35"'
                   f' fill="{CROWD[name] if n <= index else CROWD_TRACK}"/>' for n, name in enumerate(LEVELS))
    return svg(3 * size + 1.2, size, body, "pips")


def fit_pips(states, *, diameter=2.6, gap=.9):
    """Ten dots in checklist order: filled = Meets, half = Unclear, ring = Does not meet."""
    if not states:
        raise ValueError("fit pips need at least one state")
    body = []
    for number, state in enumerate(states):
        cx, cy, r = number * (diameter + gap) + diameter / 2, diameter / 2, diameter / 2 - .25
        if state == "Meets":
            body.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="{COLOUR["navy"]}"/>')
        elif state == "Unclear":
            body.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="none" stroke="{COLOUR["navy-2"]}"'
                        f' stroke-width=".3"/><path d="M{cx:.2f} {cy - r:.2f}A{r:.2f} {r:.2f} 0 0 0 {cx:.2f}'
                        f' {cy + r:.2f}Z" fill="{COLOUR["navy-2"]}"/>')
        elif state == "Does not meet":
            body.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r - .1:.2f}" fill="none" stroke="{COLOUR["grey"]}"'
                        ' stroke-width=".3"/>')
        else:
            raise ValueError("unknown checklist state")
    return svg(len(states) * (diameter + gap) - gap, diameter, "".join(body), "fitpips")


def stacked_bar(counts, colours, *, width=52.0, height=4.0):
    """A 100% stacked bar with small gaps; zero counts draw nothing."""
    total = sum(counts)
    if total <= 0 or len(counts) != len(colours) or any(type(n) is not int or n < 0 for n in counts):
        raise ValueError("stacked bar needs non-negative whole counts with a positive total")
    x, body = 0.0, []
    for count, colour in zip(counts, colours):
        segment = width * count / total
        if count:
            body.append(f'<rect x="{x + .25:.2f}" y="0" width="{max(segment - .5, .4):.2f}" height="{height}"'
                        f' fill="{colour}"/>')
        x += segment
    return svg(width, height, "".join(body), "stack")


def category_bar(count, top, *, longest=18.0, height=2.6):
    width = longest * count / top
    return svg(width, height, f'<rect width="{width:.2f}" height="{height}" rx=".6" fill="{COLOUR["navy"]}"/>', "catbar")


# Market signals ----------------------------------------------------------------------

def _months_back(day, months):
    month = day.month - months
    year = day.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    last = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return date(year, month, min(day.day, last[month - 1]))


def timeline_positions(starts, end, *, width=76.0, collision=2.7):
    """(x, row) for each start date on a 12-month axis ending on ``end``; close dots stack upwards."""
    begin = _months_back(end, 12)
    span = (end - begin).days
    x0, x1 = 1.6, width - 1.6
    placed = []
    for day in starts:
        clamped = min(max(day, begin), end)
        x = x0 + (x1 - x0) * (clamped - begin).days / span
        row = 0
        while any(abs(x - px) < collision and pr == row for px, pr in placed):
            row += 1
        placed.append((x, row))
    return begin, placed


def meta_timeline(starts, end, *, width=76.0, month_names=None, label=""):
    """One dot per advertiser at the start date of its earliest relevant ad, on the 12
    months before ``end``. ``starts`` is a sorted list of (date, recent) pairs; recent
    starts (the score's 30-day rule) are ringed maroon dots, older ones navy dots."""
    height, axis = 15.0, 10.2
    x0, x1 = 1.6, width - 1.6
    begin, placed = timeline_positions([day for day, _ in starts], end, width=width)
    span = (end - begin).days

    def xpos(day):
        return x0 + (x1 - x0) * (day - begin).days / span

    band = xpos(end - timedelta(days=30))
    body = [f'<rect x="{band:.2f}" y=".4" width="{x1 - band:.2f}" height="{axis - .4:.2f}" fill="{COLOUR["maroon-pale"]}"/>',
            f'<path d="M{x0} {axis}H{x1}" stroke="{COLOUR["line"]}" stroke-width=".3"/>']
    month = date(begin.year, begin.month, 1)
    names = month_names or {}
    while month <= end:
        if month >= begin:
            x = xpos(month)
            body.append(f'<path d="M{x:.2f} {axis}v1" stroke="{COLOUR["line"]}" stroke-width=".3"/>')
            if month.month in (1, 4, 7, 10):
                name = names.get(month.month, month.strftime("%b"))
                if month.month == 1:
                    name += " " + str(month.year)
                body.append(text(x, axis + 4.4, name, 8, anchor="middle"))
        month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    for (day, recent), (x, row) in zip(starts, placed):
        cy = axis - 3.0 - row * 2.9
        if recent:  # shape, not only colour: a ringed dot survives greyscale printing
            body.append(f'<circle cx="{x:.2f}" cy="{cy:.2f}" r="1.75" fill="{COLOUR["paper"]}" stroke="{COLOUR["maroon"]}"'
                        f' stroke-width=".4"/><circle cx="{x:.2f}" cy="{cy:.2f}" r=".9" fill="{COLOUR["maroon"]}"/>')
        else:
            body.append(f'<circle cx="{x:.2f}" cy="{cy:.2f}" r="1.25" fill="{COLOUR["navy"]}" stroke="{COLOUR["paper"]}"'
                        ' stroke-width=".45"/>')
    rows = max([row for _, row in placed], default=0)
    top = min(0.0, axis - 3.0 - rows * 2.9 - 2.0)
    return svg(width, height - top, f'<g transform="translate(0 {-top:.2f})">{"".join(body)}</g>', "timeline", label)


def spark_window_indexes(points, comparison_starts):
    """Index of each comparison week (the score's previous four then recent four) in the series."""
    starts = [point["start"] for point in points]
    if not comparison_starts or len(comparison_starts) != 8 or any(start not in starts for start in comparison_starts):
        return None
    return [starts.index(start) for start in comparison_starts]


def trends_spark(points, windows, *, width=48.0, left="", right="", label=""):
    """Weekly Google Trends interest (0-100, relative) for 12 months. ``points`` are
    {start, state, value} with state observed / bounded_nonzero ("<1") / missing; missing
    weeks break the line. ``windows`` are the eight series indexes the score compares:
    the previous four are shaded sand, the recent four rose."""
    if len(points) < 2:
        raise ValueError("a sparkline needs at least two points")
    height = 18.5
    top, bottom, x0, x1 = 1.0, 13.0, .8, width - 1.6
    step = (x1 - x0) / (len(points) - 1)

    def ypos(value):
        return bottom - (bottom - top) * value / 100

    body = []
    if windows:
        previous, recent = windows[:4], windows[4:]
        for group, colour in ((previous, COLOUR["paper-deep"]), (recent, COLOUR["maroon-pale"])):
            a = max(x0, x0 + group[0] * step - step / 2)
            b = min(x1 + .8, x0 + group[-1] * step + step / 2)
            body.append(f'<rect x="{a:.2f}" y="{top:.2f}" width="{b - a:.2f}" height="{bottom - top:.2f}" fill="{colour}"/>')
    body.append(f'<path d="M{x0} {bottom}H{x1 + .8:.2f}" stroke="{COLOUR["line"]}" stroke-width=".3"/>')
    runs, current = [], []
    for number, point in enumerate(points):
        if point["state"] == "missing":
            if current:
                runs.append(current)
            current = []
            continue
        value = point["value"] if point["state"] == "observed" else .5
        current.append((x0 + number * step, ypos(value)))
    if current:
        runs.append(current)
    for run in runs:
        line = "M" + "L".join(f"{x:.2f} {y:.2f}" for x, y in run)
        if len(run) > 1:
            body.append(f'<path d="{line}L{run[-1][0]:.2f} {bottom}L{run[0][0]:.2f} {bottom}Z" fill="{COLOUR["navy"]}"'
                        ' fill-opacity=".10"/>')
            body.append(f'<path d="{line}" fill="none" stroke="{COLOUR["navy"]}" stroke-width=".55" stroke-linejoin="round"'
                        ' stroke-linecap="round"/>')
        else:
            body.append(f'<circle cx="{run[0][0]:.2f}" cy="{run[0][1]:.2f}" r=".45" fill="{COLOUR["navy"]}"/>')
    if runs:
        ex, ey = runs[-1][-1]
        body.append(f'<circle cx="{ex:.2f}" cy="{ey:.2f}" r="1.1" fill="{COLOUR["maroon"]}" stroke="{COLOUR["paper"]}"'
                    ' stroke-width=".45"/>')
    if left:
        body.append(text(x0, bottom + 4.5, left, 8))
    if right:
        body.append(text(x1 + .8, bottom + 4.5, right, 8, anchor="end"))
    return svg(width, height, "".join(body), "spark", label)


def empty_spark(*, width=48.0, message=""):
    """The framed, empty chart area used when no usable chart exists (nothing is invented)."""
    height = 18.5
    body = (f'<rect x=".4" y="1" width="{width - .8:.2f}" height="12" rx="1" fill="none" stroke="{COLOUR["line"]}"'
            ' stroke-width=".3" stroke-dasharray="1.2 1"/>')
    if message:
        body += text(width / 2, 8.2, message, 8, anchor="middle")
    return svg(width, height, body, "spark spark--empty")
