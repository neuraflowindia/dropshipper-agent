"""Workbook styling only: the same five sheets, headers, values and row order.

Section 13 of the report redesign plan (16 October 2026). Every rule here
changes how a cell looks, never what it holds; the delivery checker reads
values only. Conditional formats are written as static XML, so the
deterministic workbook bytes stay deterministic.
"""

from openpyxl.formatting.rule import DataBarRule
from openpyxl.styles import Alignment, Border, Font, Side

from .report_theme import CROWD, LABEL_PALE

NAVY, MAROON, GREY = "24384B", "6B1F2B", "5F6062"
PALE_NAVY, PALE_MUSTARD, PALE_GREY, PALE_ROSE = "E5EAF0", "F1E5C5", "ECECEC", "F1DFE2"
SCORE_MAXIMA = {"Score": 100, "Want /20": 20, "Early /30": 30, "Room /25": 25, "Product /25": 25}
LABEL_FILLS = {"Sweet spot": LABEL_PALE["sweet_spot"][1:], "Early bet": LABEL_PALE["early_bet"][1:],
               "Proven but crowded": LABEL_PALE["proven_but_crowded"][1:]}
CROWDING_FILLS = {"Low": "F3E6E8", "Medium": "E2C3C8", "High": CROWD["low"][1:]}
STATE_FILLS = {"Meets": PALE_NAVY, "Unclear": PALE_MUSTARD, "Does not meet": PALE_GREY}
EVIDENCE_FILLS = {"Positive": PALE_NAVY, "Not-positive": PALE_GREY, "Insufficient": PALE_MUSTARD, "Unavailable": PALE_MUSTARD}
TERM_FILLS = {"Single-piece sourcing and dispatch supported": PALE_NAVY,
              "Single-piece sourcing and dispatch not supported": PALE_ROSE,
              "No subscription stated by the supplier": PALE_NAVY, "Subscription stated by the supplier": PALE_ROSE}
VALUE_FILLS = {"Shortlist": {"Label": LABEL_FILLS, "Crowding": CROWDING_FILLS},
               "Checklist": {"State": STATE_FILLS}, "Evidence": {"State": EVIDENCE_FILLS},
               "Supplier leads": {"Single-piece sourcing and dispatch": TERM_FILLS, "Subscription": TERM_FILLS}}


def _fill(style, colour):
    return style.solid(colour)


def style_sheet(style, sheet, name, headings, row_count):
    """Apply the redesign's look to one written sheet (header on row 6, data from row 7)."""
    rule = Side(style="thin", color=MAROON)
    for column in range(1, len(headings) + 1):
        cell = sheet.cell(6, column)
        cell.border = Border(bottom=rule)
    last = row_count + 6
    columns = {heading: index for index, heading in enumerate(headings, 1)}
    for row in range(7, last + 1):
        for column, heading in enumerate(headings, 1):
            cell = sheet.cell(row, column)
            value = cell.value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cell.alignment = Alignment(wrap_text=True, vertical="top", horizontal="right")
                if heading in SCORE_MAXIMA:
                    cell.number_format = "0"
            fills = VALUE_FILLS.get(name, {}).get(heading)
            if fills and isinstance(value, str) and value in fills:
                cell.fill = _fill(style, fills[value])
            if name == "Shortlist" and heading == "Checklist" and isinstance(value, str):
                cell.font = Font(name="Aptos", size=10, bold=True, color=style.CHARCOAL)
            if name == "Checklist" and heading == "Basis" and value == "inferred":
                cell.font = Font(name="Aptos", size=10, italic=True, color=GREY)
    if name == "Shortlist" and row_count:
        for heading, maximum in SCORE_MAXIMA.items():
            if heading in columns:
                letter = sheet.cell(6, columns[heading]).column_letter
                sheet.conditional_formatting.add(
                    f"{letter}7:{letter}{last}",
                    DataBarRule(start_type="num", start_value=0, end_type="num", end_value=maximum, color=NAVY,
                                showValue=True))
