"""Colour and type tokens, the report stylesheet and the embedded open-licence fonts.

Brand base (cream paper, navy, maroon, serif voice) from the approved preview;
fonts are Source Serif 4 and IBM Plex Sans subsets (SIL Open Font Licence,
texts beside the files in ``assets/fonts``), embedded as data so the report
looks the same on every phone and laptop. Type scale in points (A4) and
pixels (phone); nothing is ever smaller than 8 pt.
"""

from base64 import b64encode
from functools import lru_cache
from pathlib import Path

from .models import SafetyError

FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
FONT_LICENCES = ("source-serif-4-OFL.txt", "ibm-plex-sans-OFL.txt")
FONT_FACES = (("NF Serif", 400, "normal", "nf-serif-text-400"), ("NF Serif", 600, "normal", "nf-serif-text-600"),
              ("NF Serif", 400, "italic", "nf-serif-text-italic-400"), ("NF Serif Head", 600, "normal", "nf-serif-head-600"),
              ("NF Sans", 400, "normal", "nf-sans-400"), ("NF Sans", 500, "normal", "nf-sans-500"),
              ("NF Sans", 600, "normal", "nf-sans-600"), ("NF Sans Cond", 500, "normal", "nf-sans-cond-500"),
              ("NF Sans Cond", 600, "normal", "nf-sans-cond-600"))
MAX_FONT_BYTES = 64 * 1024

COLOUR = {"paper": "#F9F3EC", "paper-deep": "#EEE5DB", "card": "#FFFCF7", "plate": "#FFFFFF", "line": "#D8D0C8",
          "line-soft": "#E7DFD6", "ink": "#292C2F", "ink-2": "#5F6062", "navy": "#24384B", "navy-2": "#647487",
          "track": "#DCE3EA", "maroon": "#6B1F2B", "maroon-pale": "#F1DFE2", "grey": "#8E8F91"}
LABEL = {"sweet_spot": "#2A6CA3", "early_bet": "#B07A12", "proven_but_crowded": "#9A2F3E"}
LABEL_PALE = {"sweet_spot": "#E3ECF5", "early_bet": "#F3E7C9", "proven_but_crowded": "#F1DFE2"}
CROWD = {"low": "#C99AA2", "medium": "#9C4E5B", "high": "#6B1F2B"}
CROWD_TRACK = "#E9E0D7"
# Type scale (A4 points). Adjacent working levels differ by 0.5-1.5 pt.
TYPE_SCALE = {"micro": 8, "kicker": 8.5, "label": 9, "strong": 9.5, "note": 10, "body": 10.5, "lead": 11.5,
              "module": 12.5, "section": 14, "stat": 15.5, "title": 18, "kpi": 20, "display": 28}
PHONE_SCALE = {"micro": 12, "kicker": 12, "label": 13, "strong": 14, "note": 14.5, "body": 15, "lead": 16,
               "module": 17, "section": 19, "stat": 21, "title": 23, "kpi": 26, "display": 30}
MIN_PT = 8


@lru_cache(maxsize=1)
def font_css():
    """@font-face rules with every static cut embedded as a WOFF2 data URL."""
    rules = []
    for family, weight, style, name in FONT_FACES:
        path = FONT_DIR / (name + ".woff2")
        raw = path.read_bytes()
        if not raw.startswith(b"wOF2") or len(raw) > MAX_FONT_BYTES:
            raise SafetyError("REPORT_FONT_INVALID")
        rules.append(f'@font-face{{font-family:"{family}";font-weight:{weight};font-style:{style};font-display:block;'
                     f'src:url(data:font/woff2;base64,{b64encode(raw).decode("ascii")}) format("woff2");}}')
    for name in FONT_LICENCES:
        if not (FONT_DIR / name).is_file():
            raise SafetyError("REPORT_FONT_LICENCE_MISSING")
    return "".join(rules)


def css_string(value):
    """A CSS string literal for margin-box content (no markup can escape it)."""
    if not isinstance(value, str):
        raise SafetyError("REPORT_STYLE_TEXT_INVALID")
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return '"' + cleaned.replace("\\", "\\\\").replace('"', '\\"').replace("<", "\\3C ") + '"'


def _tokens(scale, unit):
    return ";".join(f"--fs-{name}:{size}{unit}" for name, size in scale.items())


def stylesheet(*, footer, page_word, of_word, marker=None):
    """The complete stylesheet: fonts, tokens, page boxes (footer, page n of N,
    optional synthetic-issue marker), print rules and the phone layout."""
    colours = ";".join(f"--{name}:{value}" for name, value in COLOUR.items())
    labels = ";".join(f"--{name}:{LABEL[name]};--{name}-pale:{LABEL_PALE[name]}" for name in LABEL)
    marker_box = ("@top-right{content:" + css_string(marker.upper() if marker else "") + ';font-family:"NF Sans Cond","Arial Narrow",sans-serif;'
                  "font-weight:600;font-size:8pt;letter-spacing:.08em;color:#6B1F2B;vertical-align:bottom;"
                  "padding-bottom:4mm}") if marker else ""
    pages = ("@page{size:A4 portrait;margin:15mm 16mm 17mm 16mm;background:#F9F3EC;" + marker_box
             + "@bottom-left{content:" + css_string(footer) + ';font-family:"NF Sans","Segoe UI",Arial,sans-serif;'
             "font-size:8pt;color:#5F6062;vertical-align:top;padding-top:6mm}"
             "@bottom-right{content:" + css_string(page_word + " ") + " counter(page) " + css_string(" " + of_word + " ")
             + ' counter(pages);font-family:"NF Sans","Segoe UI",Arial,sans-serif;font-size:8pt;color:#5F6062;'
             "vertical-align:top;padding-top:6mm}}"
             "@page cover{margin:0;@top-right{content:none}@bottom-left{content:none}@bottom-right{content:none}}")
    return (font_css() + ":root{" + colours + ";" + labels + ";" + _tokens(TYPE_SCALE, "pt") + ";"
            '--serif:"NF Serif",Georgia,serif;--head:"NF Serif Head","NF Serif",Georgia,serif;'
            '--sans:"NF Sans","Segoe UI",Arial,sans-serif;--cond:"NF Sans Cond","Arial Narrow",sans-serif}'
            + pages + BASE_CSS + "@media screen and (max-width:640px){:root{" + _tokens(PHONE_SCALE, "px") + "}"
            + PHONE_CSS + "}")


BASE_CSS = r"""
*{box-sizing:border-box}
html{background:var(--paper)}
body{margin:0;color:var(--ink);font-family:var(--sans);font-size:var(--fs-label);line-height:1.4;font-kerning:normal;
  text-rendering:optimizeLegibility;-webkit-print-color-adjust:exact;print-color-adjust:exact;background:var(--paper)}
h1,h2,h3,p,ul,ol,dl,dd,figure{margin:0;padding:0}
ul,ol{list-style:none}
a{color:inherit;text-decoration:none}
b,strong{font-weight:600}
svg{display:block;overflow:visible}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.soft{color:var(--ink-2)}
.nw{white-space:nowrap}
p,li,dd,td,th{overflow-wrap:break-word}

.kicker{font-family:var(--cond);font-weight:600;font-size:var(--fs-kicker);letter-spacing:.08em;text-transform:uppercase;color:var(--maroon);line-height:1.25;margin-bottom:1.6mm}
.h-display{font-family:var(--head);font-weight:600;font-size:var(--fs-display);line-height:1.08;color:var(--navy);letter-spacing:-.005em;text-wrap:balance;hyphens:none}
.h-title{font-family:var(--head);font-weight:600;font-size:var(--fs-title);line-height:1.15;color:var(--navy);text-wrap:balance;hyphens:none;overflow-wrap:break-word}
.h-section{font-family:var(--head);font-weight:600;font-size:var(--fs-section);line-height:1.2;color:var(--navy);margin-bottom:2mm;break-after:avoid;page-break-after:avoid}
.h-module{font-family:var(--sans);font-weight:600;font-size:var(--fs-strong);line-height:1.3;color:var(--navy);margin-bottom:2mm;break-after:avoid;page-break-after:avoid}
.dek{font-family:var(--serif);font-size:var(--fs-lead);line-height:1.45;color:var(--ink);max-width:150mm;margin-top:2.4mm}
.stat{font-family:var(--sans);font-weight:600;font-size:var(--fs-stat);line-height:1.1;color:var(--navy)}

.chip{display:inline-flex;align-items:center;gap:1.3mm;min-height:5.6mm;padding:.6mm 2.4mm .6mm 1.8mm;border-radius:2.8mm;font-family:var(--sans);font-weight:600;font-size:var(--fs-label);color:var(--ink);line-height:1.15}
.chip--sm{min-height:4.8mm;padding:.5mm 2mm .5mm 1.5mm;font-size:var(--fs-micro);gap:1.1mm}
.chip--sweet_spot{background:var(--sweet_spot-pale)}.chip--early_bet{background:var(--early_bet-pale)}.chip--proven_but_crowded{background:var(--proven_but_crowded-pale)}
.chip--outline{background:transparent;box-shadow:inset 0 0 0 .25mm var(--line);padding:.6mm 2.4mm;font-weight:500}
.chip--role{min-height:4.4mm;padding:.4mm 1.6mm;font-size:var(--fs-micro);font-weight:500;color:var(--navy);box-shadow:inset 0 0 0 .25mm var(--navy-2)}
.chip--comply{background:transparent;color:var(--maroon);box-shadow:inset 0 0 0 .3mm var(--maroon)}
.glyph{flex:none}

@media screen{
  html{background:#CFC9C2}body{padding:8mm 0;background:#CFC9C2}
  .sheet{position:relative;width:210mm;min-height:297mm;margin:0 auto 8mm;padding:15mm 16mm 17mm;background:var(--paper);box-shadow:0 .4mm 1.2mm rgba(0,0,0,.10),0 3mm 9mm rgba(0,0,0,.08)}
  .sheet.cover{padding:0;height:297mm}
  .screen-foot{position:absolute;left:16mm;right:16mm;bottom:7mm;display:flex;justify-content:space-between;gap:6mm;font-size:var(--fs-micro);color:var(--ink-2)}
}
@media print{
  .screen-foot{display:none}
  .sheet{break-after:page;page-break-after:always}.sheet:last-child{break-after:auto;page-break-after:auto}
  .cover{page:cover;height:297mm;overflow:hidden}
}

.cover{display:flex;flex-direction:column;background:var(--paper)}
.cover-inner{padding:15mm 16mm 0;flex:1}
.cover-top{display:flex;justify-content:space-between;align-items:center;gap:6mm}
.logo{width:42mm;height:auto}
.logo-text{font-family:var(--head);font-weight:600;font-size:var(--fs-module);color:var(--maroon)}
.cover-issue{font-family:var(--cond);font-weight:600;font-size:var(--fs-kicker);letter-spacing:.08em;text-transform:uppercase;color:var(--maroon);text-align:right}
.cover-title{margin-top:16mm;max-width:156mm}
.cover-title .kicker{margin-bottom:3mm}
.cover-dek{font-family:var(--serif);font-size:var(--fs-lead);line-height:1.5;margin-top:4mm;max-width:150mm}
.cover-kpis,.kpis{display:grid;grid-template-columns:repeat(4,1fr);border-top:.3mm solid var(--line);border-bottom:.3mm solid var(--line)}
.cover-kpis{margin-top:8mm}
.kpis{margin-bottom:5mm}
.kpi{padding:2.6mm 0 2.8mm 4mm;border-left:.3mm solid var(--line-soft)}
.cover-kpis .kpi{padding:3.2mm 0 3.4mm 4mm}
.kpi:first-child{padding-left:0;border-left:0}
.kpi b{display:block;font-weight:600;font-size:var(--fs-kpi);line-height:1.1;color:var(--navy);font-variant-numeric:tabular-nums}
.kpi span{display:block;font-size:var(--fs-label);color:var(--ink-2);line-height:1.3}
.mosaic{display:grid;grid-template-columns:repeat(3,1fr);gap:5mm 4mm;margin-top:8mm}
.mosaic-tile{display:block}
.mosaic .plate{height:50mm}
.mosaic .plate figcaption{display:none}
.mosaic .plate--empty figcaption{display:block}
.mosaic-cap{display:grid;grid-template-columns:auto 1fr;column-gap:1.8mm;row-gap:1.2mm;margin-top:2.2mm;align-items:baseline}
.mosaic-rank{font-family:var(--cond);font-weight:600;font-size:var(--fs-strong);color:var(--maroon)}
.mosaic-title{font-weight:600;font-size:var(--fs-label);line-height:1.3;color:var(--ink)}
.mosaic-score{grid-column:2;display:flex;flex-wrap:wrap;align-items:center;gap:1.8mm}
.mosaic-score b{font-size:var(--fs-strong);color:var(--navy)}
.cover-foot{background:var(--navy);color:#EFE8DF;padding:6mm 16mm 7mm;display:flex;justify-content:space-between;align-items:flex-end;gap:10mm;font-size:var(--fs-label);line-height:1.45}
.cover-foot p:first-child{max-width:125mm}
.cover-marker{font-family:var(--cond);font-weight:600;font-size:var(--fs-micro);letter-spacing:.08em;text-transform:uppercase;color:#E9C9CE;white-space:nowrap}

.plate{position:relative;background:var(--plate);border-radius:2mm;display:flex;align-items:center;justify-content:center;padding:2.5mm;border:.25mm solid var(--line-soft);overflow:hidden}
.plate img{max-width:100%;max-height:100%;object-fit:contain;border-radius:1mm}
.plate--empty{background:var(--paper-deep);border-color:transparent;flex-direction:column;gap:2.5mm;color:var(--navy-2);text-align:center}
.plate--empty figcaption{position:static;color:var(--ink-2);font-size:var(--fs-micro);line-height:1.35;padding:0 3mm}
.plate--empty figcaption b{display:block;font-weight:600;color:var(--navy-2)}
.plate--big{height:50mm}
.plate--big.plate--empty figcaption{font-size:var(--fs-label)}
.plate--big.plate--empty figcaption span{display:block;font-size:var(--fs-micro);margin-top:1mm}
.plate-note{position:absolute;right:1.6mm;bottom:1.4mm;padding:.4mm 1.4mm;border-radius:.8mm;background:rgba(255,255,255,.9);font-size:var(--fs-micro);color:var(--ink-2);line-height:1.3;max-width:44mm;text-align:right}

.page-head{margin-bottom:5mm}

.mixes{display:grid;grid-template-columns:1fr 1fr 1fr;gap:7mm;margin-bottom:5mm}
.mix-title{font-weight:600;font-size:var(--fs-label);color:var(--navy);margin-bottom:1.8mm}
.mix .stack{margin-bottom:2mm}
.mix-legend li{display:flex;align-items:center;gap:1.6mm;font-size:var(--fs-label);line-height:1.6}
.mix-legend li span{flex:1}
.mix-legend li b,.cat-list b{font-variant-numeric:tabular-nums}
.mix-legend i{display:inline-block;width:2.8mm;height:2.8mm;border-radius:.4mm;flex:none}
.cat-list li{display:grid;grid-template-columns:31mm 1fr auto;align-items:center;gap:1.6mm;font-size:var(--fs-label);line-height:1.6}
.glance--dense .mixes{margin-bottom:3.5mm}
.glance--dense .mix-legend{display:flex;flex-wrap:wrap;gap:0 3mm}
.glance--dense .league td{padding-top:1.1mm;padding-bottom:1.1mm}

.league{width:100%;border-collapse:collapse;table-layout:fixed}
.league thead{display:table-header-group}
.league thead th{font-family:var(--cond);font-weight:600;font-size:var(--fs-micro);letter-spacing:.06em;text-transform:uppercase;color:var(--ink-2);text-align:left;padding:0 2mm 1.6mm 0;border-bottom:.3mm solid var(--line)}
.league td{padding:1.5mm 2mm 1.5mm 0;border-bottom:.25mm solid var(--line-soft);vertical-align:middle}
.league tr{break-inside:avoid;page-break-inside:avoid}
.league .c0{width:8mm;font-family:var(--cond);font-weight:600;font-size:var(--fs-strong);color:var(--maroon)}
.league .c1{width:auto}.league .c2{width:38mm}.league .c3{width:36mm}.league .c4{width:23mm}.league .c5{width:12mm;text-align:right;padding-right:0}
.league .chip--sm{border-radius:2.4mm}
.league thead .c5{text-align:right;padding-right:0}
.league .pt{display:block;font-weight:600;font-size:var(--fs-strong);line-height:1.25;color:var(--ink)}
.league .pt a{border-bottom:.25mm solid rgba(36,56,75,.35)}
.league .meta{display:block;font-size:var(--fs-micro);color:var(--ink-2);margin-top:.4mm}
.shield{color:var(--maroon);display:inline-block;vertical-align:-.5mm}
.shield .icon{display:inline-block}
.scorecell{display:flex;align-items:center;gap:2.2mm}
.scorecell b{font-size:var(--fs-strong);color:var(--navy);font-variant-numeric:tabular-nums}
.crowdcell{display:flex;align-items:center;gap:1.6mm;font-size:var(--fs-label)}
.league .c5{font-weight:600;font-size:var(--fs-strong);color:var(--navy);font-variant-numeric:tabular-nums}
.league .of{font-weight:400;font-size:var(--fs-label);color:var(--ink-2)}
.league tr.group td{padding:3.2mm 0 1.2mm;border-bottom:.3mm solid var(--line);font-family:var(--cond);font-weight:600;font-size:var(--fs-micro);letter-spacing:.06em;text-transform:uppercase;color:var(--maroon)}
.table-key{display:flex;flex-wrap:wrap;align-items:center;gap:1mm;margin-top:2.6mm;font-size:var(--fs-micro);color:var(--ink-2)}
.table-key .icon{color:var(--maroon)}

.pa-head{display:grid;grid-template-columns:50mm 1fr 30mm;column-gap:6mm;align-items:start;padding-bottom:3.6mm;border-bottom:.3mm solid var(--line);margin-bottom:3.4mm}
.pa-photo{position:relative}
.rank-badge{position:absolute;left:0;top:0;min-width:10mm;height:8mm;padding:0 2mm;display:flex;align-items:center;justify-content:center;background:var(--maroon);color:#fff;font-family:var(--cond);font-weight:600;font-size:var(--fs-module);border-radius:2mm 0 2mm 0}
.pa-titles{padding-top:.6mm;min-width:0}
.chips{display:flex;flex-wrap:wrap;gap:1.6mm;margin-top:3mm}
.pa-dial{display:flex;flex-direction:column;align-items:center;gap:1.2mm}
.pa-dial p{font-weight:600;font-size:var(--fs-label);color:var(--navy);text-align:center;line-height:1.25}
.pa-dial p.stat{font-size:var(--fs-kpi);line-height:1.1;margin-top:2mm}
.label-line{display:flex;gap:2mm;align-items:flex-start;font-size:var(--fs-label);line-height:1.4;margin-top:3mm;max-width:85mm}
.label-line .glyph{margin-top:.5mm}
.changed{margin-top:2.4mm;padding-left:2.6mm;border-left:.6mm solid var(--maroon);font-size:var(--fs-label);line-height:1.4;max-width:88mm}
.changed b{color:var(--maroon)}
.adds-up{margin-bottom:3mm;padding-top:.6mm}
.parts{display:grid;grid-template-columns:repeat(4,1fr);gap:5mm}
.part-head{display:flex;justify-content:space-between;align-items:baseline;gap:2mm;font-size:var(--fs-label);margin-bottom:1.2mm}
.part-head>b{color:var(--navy);font-size:var(--fs-strong)}
.part-val{white-space:nowrap;color:var(--ink-2);font-variant-numeric:tabular-nums}
.part-val b{color:var(--navy);font-size:var(--fs-strong)}
.part-items{font-size:var(--fs-micro);color:var(--ink-2);margin-top:1.3mm;line-height:1.4}
.part-items b{color:var(--ink);font-weight:600}
.comply{display:grid;grid-template-columns:4.4mm 1fr;gap:2.6mm;align-items:start;padding:1.8mm 3.6mm;margin:0 0 3mm;background:var(--maroon-pale);border-left:1mm solid var(--maroon);border-radius:0 1.6mm 1.6mm 0;color:var(--maroon);break-inside:avoid;page-break-inside:avoid}
.comply p{font-size:var(--fs-label);line-height:1.4;color:var(--ink)}
.comply b{font-weight:600;color:var(--maroon)}
.why{margin-bottom:3mm}
.lead{font-family:var(--serif);font-size:var(--fs-body);line-height:1.47;max-width:168mm}
.points{counter-reset:pt;margin-top:1.6mm;display:grid;gap:1mm;max-width:168mm}
.points li{counter-increment:pt;display:grid;grid-template-columns:6mm 1fr;font-family:var(--serif);font-size:var(--fs-note);line-height:1.45;break-inside:avoid;page-break-inside:avoid}
.points li::before{content:counter(pt);font-family:var(--cond);font-weight:600;font-size:var(--fs-strong);color:var(--maroon);line-height:1.6}
.note{margin-top:2.4mm;padding-top:2mm;border-top:.3mm solid var(--line);break-inside:avoid;page-break-inside:avoid}
.note-label{font-family:var(--cond);font-weight:600;font-size:var(--fs-kicker);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-2);margin-bottom:1mm}
.note-text{font-family:var(--serif);font-style:italic;font-size:var(--fs-note);line-height:1.45}
.fit-head{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:1mm 4mm;padding-top:2.2mm;border-top:.3mm solid var(--line);margin-bottom:1.6mm;break-after:avoid;page-break-after:avoid}
.fit-head .h-section{margin-bottom:0}
.fit-sum{display:flex;align-items:center;gap:2.4mm;font-size:var(--fs-label);color:var(--ink-2)}
.fit-sum b{color:var(--navy)}
.fit-list{columns:2;column-gap:6.5mm}
.fit-list li{break-inside:avoid;page-break-inside:avoid;display:grid;grid-template-columns:3.8mm 1fr;gap:2.2mm;margin-bottom:1.1mm}
.fit-list .glyph{margin-top:.3mm}
.crit{display:flex;flex-wrap:wrap;align-items:baseline;gap:0 1.6mm;font-size:var(--fs-strong);line-height:1.3}
.crit b{color:var(--ink)}
.crit-state{font-size:var(--fs-label);color:var(--maroon)}
.est{display:inline-block;font-size:var(--fs-micro);line-height:1;padding:.7mm 1.3mm;border-radius:1mm;box-shadow:inset 0 0 0 .25mm var(--navy-2);color:var(--navy-2);font-weight:500}
.crit-why{font-size:var(--fs-label);line-height:1.4;color:var(--ink);margin-top:.4mm}

.pb-head{margin-bottom:3.4mm;break-after:avoid;page-break-after:avoid}
.pb-strip{display:flex;align-items:center;flex-wrap:wrap;gap:1.4mm 2.6mm;padding-bottom:2.6mm;border-bottom:.3mm solid var(--line)}
.pb-rank{font-family:var(--cond);font-weight:600;font-size:var(--fs-module);color:var(--maroon)}
.pb-title{font-family:var(--head);font-weight:600;font-size:var(--fs-module);line-height:1.25;color:var(--navy);flex:1;min-width:60mm}
.pb-sec{margin-left:2.6mm;font-family:var(--cond);font-weight:600;font-size:var(--fs-kicker);letter-spacing:.08em;text-transform:uppercase;color:var(--maroon);vertical-align:.4mm;white-space:nowrap}
.pb-score{font-size:var(--fs-label);color:var(--ink-2);white-space:nowrap}
.pb-score b{font-size:var(--fs-module);color:var(--navy)}
.tiles{display:grid;gap:3.2mm;margin-bottom:3.2mm}
.tiles--2{grid-template-columns:1fr 1fr}.tiles--3{grid-template-columns:1fr 1fr 1fr}
.tile{background:var(--card);border-radius:2mm;padding:2.8mm 3.4mm 3mm;border:.25mm solid var(--line-soft);break-inside:avoid;page-break-inside:avoid;min-width:0}
.tile-title{font-weight:600;font-size:var(--fs-strong);color:var(--navy);line-height:1.3}
.tile-src{font-size:var(--fs-micro);color:var(--ink-2);margin:.2mm 0 2mm;line-height:1.35}
.tile-note{font-size:var(--fs-micro);color:var(--ink-2);line-height:1.45;margin-top:1.6mm}
.tile-note .glyph{display:inline-block;vertical-align:-.5mm}
.crowd-body{display:grid;grid-template-columns:31mm 1fr;gap:4mm;align-items:start}
.crowd-left .stat{margin-top:1.2mm}
.crowd-left .soft{font-size:var(--fs-micro);margin-top:.4mm}
.facts{display:grid;grid-template-columns:1fr;gap:1.5mm;padding-left:4mm;border-left:.25mm solid var(--line-soft)}
.facts>div{font-size:var(--fs-label);line-height:1.35}
.facts dt,.facts dd{display:inline}
.facts dt::after{content:": "}
.facts dt{color:var(--ink-2)}
.facts dd{font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}
.facts dd .soft{font-weight:400}
.stat-line{display:flex;align-items:baseline;flex-wrap:wrap;gap:1.4mm;margin-bottom:1.6mm;font-size:var(--fs-label)}
.stat-line .soft{flex-basis:100%}
.legend{display:flex;flex-wrap:wrap;gap:1mm 4mm;margin-top:1.4mm;font-size:var(--fs-micro);color:var(--ink-2)}
.legend span{display:inline-flex;align-items:center;gap:1.2mm}
.dot{display:inline-block;width:2.5mm;height:2.5mm;border-radius:50%;flex:none}
.dot--navy{background:var(--navy)}.dot--maroon{background:var(--maroon);width:1.5mm;height:1.5mm;box-shadow:0 0 0 .55mm var(--paper),0 0 0 .9mm var(--maroon);margin:0 .5mm}
.trend-state{display:inline-flex;align-items:center;gap:1.2mm;font-weight:600;font-size:var(--fs-strong);color:var(--navy);margin-bottom:1.6mm}
.trend-state--up::before{content:"";width:0;height:0;border-left:1.3mm solid transparent;border-right:1.3mm solid transparent;border-bottom:2.2mm solid var(--maroon)}
.trend-state--flat::before{content:"";width:2.6mm;height:.6mm;background:var(--navy-2);border-radius:.3mm}
.trend-state--none{color:var(--ink-2)}
.plat li{display:grid;grid-template-columns:auto 1fr auto;grid-template-areas:"name state price" "roles roles roles";column-gap:2mm;row-gap:.9mm;align-items:center;padding:1.5mm 0;border-bottom:.25mm solid var(--line-soft)}
.plat li:first-child{padding-top:0}
.p-name{grid-area:name;font-weight:600;font-size:var(--fs-label)}
.p-state{grid-area:state;display:flex;align-items:center;gap:1.2mm;font-size:var(--fs-micro);color:var(--ink-2)}
.p-roles{grid-area:roles;display:flex;flex-wrap:wrap;gap:1mm}
.p-roles:empty{display:none}
.p-price{grid-area:price;font-weight:600;font-size:var(--fs-label);color:var(--navy);font-variant-numeric:tabular-nums}
.qc{width:100%;border-collapse:collapse;font-size:var(--fs-micro);table-layout:fixed}
.qc th{font-weight:500;color:var(--ink-2);text-align:center;padding:0 .3mm 1.2mm;line-height:1.2}
.qc thead th{font-family:var(--cond);overflow-wrap:normal;hyphens:none}
.qc thead th:first-child{width:15.5mm}
.qc tbody th{text-align:left;font-weight:600;color:var(--ink);font-size:var(--fs-label);padding:1.1mm 0;border-top:.25mm solid var(--line-soft)}
.qc td{text-align:center;padding:1.1mm 0;border-top:.25mm solid var(--line-soft)}
.qc td .glyph{margin:0 auto}
.legend--qc{gap:.6mm 3mm}
.pb-bottom{display:grid;grid-template-columns:2fr 1fr;gap:6mm;margin-top:1mm}
.prices-sec,.ex-sec{min-width:0}
.prices{display:grid;grid-template-columns:1fr 1fr;gap:6mm}
.sub{font-family:var(--cond);font-weight:600;font-size:var(--fs-kicker);letter-spacing:.06em;text-transform:uppercase;color:var(--ink-2);margin-bottom:.6mm;line-height:1.25}
.price-list li{display:grid;grid-template-columns:auto 1fr;column-gap:2.4mm;padding:1.1mm 0;border-top:.25mm solid var(--line-soft);font-size:var(--fs-label);line-height:1.35;break-inside:avoid;page-break-inside:avoid}
.price-list .price{grid-row:span 3;font-size:var(--fs-module);color:var(--navy);line-height:1.15;max-width:24mm;overflow-wrap:anywhere}
.price-list .price--text{font-size:var(--fs-label)}
.price-list .price--none{font-weight:400;font-size:var(--fs-micro);color:var(--ink-2)}
.price-list .soft{font-size:var(--fs-micro)}
.price-list .p-what{overflow-wrap:anywhere}
.price-list .p-what a{border-bottom:.25mm solid rgba(36,56,75,.35)}
.price-empty{font-size:var(--fs-label);color:var(--ink-2);padding:1.1mm 0;border-top:.25mm solid var(--line-soft)}
.bulk{margin-top:2mm;padding:1.6mm 2.6mm;line-height:1.4;border-left:.8mm solid var(--maroon);background:var(--maroon-pale);font-size:var(--fs-label);color:var(--ink);break-inside:avoid;page-break-inside:avoid}
.thumbs{display:flex;gap:2.4mm;margin-bottom:2.4mm}
.thumb{width:24mm;height:24mm;padding:1.2mm;border-radius:1.6mm}
.thumb img{border-radius:.6mm}
.ex-list li{display:grid;grid-template-columns:1fr auto;column-gap:2mm;padding:1.4mm 0;border-top:.25mm solid var(--line-soft);font-size:var(--fs-label);line-height:1.35;break-inside:avoid;page-break-inside:avoid}
.ex-list .soft{grid-column:1/-1;font-size:var(--fs-micro)}
.ex-price{font-weight:600;color:var(--navy);font-variant-numeric:tabular-nums;text-align:right;max-width:22mm;overflow-wrap:anywhere}
.ex-list a{border-bottom:.25mm solid rgba(36,56,75,.35)}
.sup-sec{margin-top:3.8mm}
.sup{width:100%;border-collapse:collapse;table-layout:fixed;font-size:var(--fs-micro)}
.sup thead{display:table-header-group}
.sup thead th{font-family:var(--cond);font-weight:600;font-size:var(--fs-micro);letter-spacing:.02em;text-transform:uppercase;color:var(--ink-2);text-align:left;vertical-align:bottom;line-height:1.2;padding:0 1.4mm 1.4mm 0;border-bottom:.3mm solid var(--line);overflow-wrap:normal;hyphens:none}
.sup thead th.s-name{font-family:var(--sans);font-size:var(--fs-strong);letter-spacing:0;text-transform:none;color:var(--navy);width:44mm}
.sup thead th.s-moq{width:19mm}
.sup tr,.sup tbody{break-inside:avoid;page-break-inside:avoid}
.sup-row--terms>tr:first-child>*{border-bottom:0;padding-bottom:.6mm}
.sup-terms td{padding:0 0 1.4mm;border-bottom:.25mm solid var(--line-soft);font-size:var(--fs-micro);line-height:1.4;color:var(--ink-2)}
.s-links{margin-top:.8mm;font-size:var(--fs-micro);line-height:1.4}
.s-links a{color:var(--maroon);border-bottom:.25mm solid rgba(107,31,43,.35)}
.sup tbody th,.sup td{padding:1.4mm 1.6mm 1.4mm 0;border-bottom:.25mm solid var(--line-soft);text-align:left;vertical-align:top;font-weight:400}
.sup-cont{width:100%;border-collapse:collapse;table-layout:fixed;margin-top:1mm}
.sup-cont thead{display:table-header-group}
.sup-cont thead th{text-align:left;font-weight:600;font-size:var(--fs-strong);line-height:1.3;color:var(--navy);padding:0 0 1.2mm;border-bottom:.3mm solid var(--line)}
.sup-cont td{padding:0}
.sup+.sup-cont{margin-top:3mm}
.sup-block{padding:1.4mm 0;border-bottom:.25mm solid var(--line-soft)}
.sup-block .s-head{font-size:var(--fs-label);color:var(--ink);margin-bottom:.6mm;break-after:avoid;page-break-after:avoid}
.sup-block .s-head b{font-weight:600}
.sup-block .s-head .soft{font-size:var(--fs-micro)}
.sup-block ul{display:grid;gap:.6mm}
.sup-block li{font-size:var(--fs-micro);line-height:1.4;color:var(--ink-2);break-inside:avoid;page-break-inside:avoid}
.sup-block li:first-child{color:var(--ink)}
.sup .s-name b{display:block;font-size:var(--fs-label);color:var(--ink);line-height:1.3}
.sup .s-name span{display:block;color:var(--ink-2);margin-top:.3mm}
.sup .s-name a{border-bottom:.25mm solid rgba(107,31,43,.35)}
.sup .s-moq b{font-size:var(--fs-label);color:var(--navy);overflow-wrap:anywhere}
.term{display:inline-flex;align-items:center;gap:1.1mm;color:var(--ink)}

.sup-note,.sup-empty{font-size:var(--fs-micro);color:var(--ink-2);margin-top:2mm}
.sup-empty{font-size:var(--fs-label);color:var(--ink)}
.src-sec{margin-top:3.2mm;break-inside:avoid;page-break-inside:avoid}
.src-sec .h-module{margin-bottom:1.2mm}
.sources{display:flex;flex-wrap:wrap;gap:.8mm 4mm;font-size:var(--fs-micro);color:var(--ink-2)}
.sources a{color:var(--maroon);border-bottom:.25mm solid rgba(107,31,43,.35)}

.key-grid{display:grid;grid-template-columns:1fr 1fr;gap:8mm}
.key-score-block{padding-bottom:1mm;margin-bottom:4mm;border-bottom:.3mm solid var(--line)}
.key-score{display:grid;grid-template-columns:30mm 1fr;gap:5mm;align-items:center;margin-bottom:2.6mm;max-width:160mm}
.key-parts{display:grid;grid-template-columns:repeat(4,1fr);gap:5mm}
.key-parts li{font-size:var(--fs-label);line-height:1.4;padding-top:1.6mm;border-top:.6mm solid var(--navy)}
.key-block{margin-bottom:3.6mm;break-inside:avoid;page-break-inside:avoid}
.key-score p,.key-crowd p,.key-p,.key-comply p{font-size:var(--fs-label);line-height:1.45}
.key-parts b{color:var(--navy);font-size:var(--fs-strong)}
.key-parts .max{font-size:var(--fs-micro);color:var(--ink-2)}
.key-labels li{display:grid;grid-template-columns:38mm 1fr;gap:2mm;align-items:start;margin-bottom:1.8mm;font-size:var(--fs-label);line-height:1.4}
.key-labels .chip{justify-self:start}
.key-crowd{display:grid;grid-template-columns:28mm 1fr;gap:4mm;align-items:center}
.key-states li{display:grid;grid-template-columns:auto 1fr;gap:2.2mm;align-items:center;font-size:var(--fs-label);line-height:1.4;margin-bottom:1.6mm}
.key-states .est{justify-self:start}
.key-pips{display:flex;align-items:center;gap:2.4mm;margin-top:2mm;font-size:var(--fs-micro);color:var(--ink-2)}
.key-signals li{display:grid;grid-template-columns:17mm 1fr;gap:2.4mm;align-items:start;font-size:var(--fs-label);line-height:1.4;margin-bottom:2.2mm}
.sig{display:flex;flex-wrap:wrap;gap:1.2mm;align-items:center;padding-top:.4mm}
.band{display:inline-block;width:4mm;height:3mm}
.band--sand{background:var(--paper-deep)}.band--rose{background:var(--maroon-pale)}
.key-p .glyph{display:inline-block;vertical-align:-.6mm}
.key-comply{display:grid;gap:1.6mm}
.key-comply .chip{justify-self:start}
.key-not-line{margin-top:1mm;padding:2.6mm 4mm;background:var(--paper-deep);border-radius:1.6mm;font-size:var(--fs-label);line-height:1.45;break-inside:avoid;page-break-inside:avoid}

.steps{display:grid;grid-template-columns:repeat(4,1fr);gap:5mm;margin:2mm 0 7mm}
.steps li{padding-top:3mm;border-top:.6mm solid var(--navy);font-size:var(--fs-label);line-height:1.45}
.step-n{display:inline-flex;align-items:center;justify-content:center;width:6mm;height:6mm;border-radius:50%;background:var(--maroon);color:#fff;font-weight:600;font-size:var(--fs-label);margin-bottom:2mm}
.step-t{font-family:var(--head);font-weight:600;font-size:var(--fs-module);color:var(--navy);margin-bottom:1mm}
.close-grid{display:grid;grid-template-columns:1.1fr 1fr;gap:8mm;margin-bottom:6mm}
.sites{display:flex;flex-wrap:wrap;gap:1.6mm}
.sites li{font-size:var(--fs-label);padding:.9mm 2.2mm;border-radius:1.2mm;background:var(--card);border:.25mm solid var(--line-soft)}
.issue-facts div{display:grid;grid-template-columns:30mm 1fr;gap:2mm;padding:1.3mm 0;border-top:.25mm solid var(--line-soft);font-size:var(--fs-label)}
.issue-facts dt{color:var(--ink-2)}
.issue-facts dd{font-weight:600}
.notes-box{padding:4.5mm 5mm;background:var(--paper-deep);border-radius:2mm;break-inside:avoid;page-break-inside:avoid}
.notes-box ul{display:grid;gap:1.8mm}
.notes-box li{position:relative;padding-left:3.6mm;font-size:var(--fs-label);line-height:1.45}
.notes-box li::before{content:"";position:absolute;left:0;top:1.7mm;width:1.4mm;height:1.4mm;border-radius:50%;box-shadow:inset 0 0 0 .3mm var(--maroon)}
.synthetic-note{margin-top:5mm;font-size:var(--fs-micro);color:var(--maroon)}
"""

PHONE_CSS = r"""
  html,body{background:var(--paper)}body{padding:0}
  .sheet{width:auto;min-height:0;margin:0;padding:24px 16px 40px;box-shadow:none;border-bottom:8px solid #E2DBD3}
  .sheet.cover{height:auto;padding:0}
  .screen-foot{position:static;margin-top:20px;gap:12px;font-size:11px;flex-wrap:wrap}
  .cover-inner{padding:20px 16px 0}.logo{width:120px}
  .cover-title{margin-top:28px}
  .cover-kpis,.kpis{grid-template-columns:1fr 1fr}
  .kpi:nth-child(3){padding-left:0;border-left:0}
  .kpi:nth-child(n+3){border-top:1px solid var(--line-soft)}
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
  .plate--big{height:auto;aspect-ratio:1/1}
  .pa-dial{flex-direction:row;justify-content:flex-start;gap:14px}
  .pa-dial .dial{width:120px;height:120px}
  .label-line,.changed{max-width:none}
  .parts{grid-template-columns:1fr 1fr;gap:18px 16px}
  .partbar{width:100%;height:8px}
  .fit-head{flex-direction:column;align-items:flex-start;gap:8px}
  .fit-list{columns:1}
  .fitpips{width:150px;height:auto}
  .tiles--2,.tiles--3,.prices,.key-grid,.close-grid,.steps,.pb-bottom{grid-template-columns:1fr}
  .pb-bottom{gap:16px}
  .key-labels li{grid-template-columns:1fr;gap:4px}
  .timeline{width:100%;height:auto}
  .spark{width:100%;max-width:300px;height:auto}
  .gauge{width:130px;height:auto}
  .key-score{grid-template-columns:110px 1fr}.key-score .dial{width:110px;height:110px}
  .key-crowd{grid-template-columns:110px 1fr}.key-crowd .gauge{width:110px}
  .key-parts{grid-template-columns:1fr 1fr;gap:14px}
  .sup,.sup tbody,.sup tr,.sup th,.sup td{display:block;width:auto!important}
  .sup-terms td::before{content:none}
  .sup thead{display:none}
  .sup tr{padding:12px 14px;margin-bottom:10px;background:var(--card);border-radius:8px;box-shadow:0 0 0 1px var(--line-soft)}
  .sup tbody th,.sup td{border:0;padding:3px 0}
  .sup td{display:flex;justify-content:space-between;gap:12px}
  .sup td::before{content:attr(data-term);color:var(--ink-2)}
  .pb-title{flex:1 1 calc(100% - 50px)}
  .pb-sec{display:block;margin:4px 0 0}
"""
