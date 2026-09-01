"""Generate the HTML fixture variants used by the scraper resilience tests.

The live page is client-rendered and ships an empty table, so a server-rendered
HTML fixture has to be synthesised.  Each variant applies one realistic kind of
upstream change; the parser is expected to keep working across A-E and to fail
*safely* on F.

    A  baseline server-rendered table
    B  every CSS class and id renamed
    C  extra wrapper elements around table, rows and cells
    D  columns reordered
    E  a expected column (GMP %) removed entirely
    F  structurally unrecognisable content

Run from the project root::

    python scripts/generate_html_fixtures.py
"""

from __future__ import annotations

import html
import json
import pathlib
import random

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "html"
API_FIXTURE = ROOT / "tests" / "fixtures" / "api" / "investorgain_report.json"

# Column label -> callable pulling the display value out of an API row.
COLUMNS: list[tuple[str, str]] = [
    ("IPO Name", "~ipo_name"),
    ("Type", "~IPO_Category"),
    ("GMP", "GMP"),
    ("GMP %", "~gmp_percent_calc"),
    ("Price", "Price (₹)"),
    ("IPO Size", "IPO Size"),
    ("Lot", "Lot"),
    ("Sub", "Sub"),
    ("Open", "~Srt_Open"),
    ("Close", "~Srt_Close"),
    ("BoA Dt", "~Srt_BoA_Dt"),
    ("Listing", "~Str_Listing"),
    ("Status", "~ipo_status1"),
    ("Id", "~id"),
]


def _cell(row: dict, key: str) -> str:
    value = row.get(key, "")
    if value is None:
        return ""
    return str(value)


def _rows() -> list[dict]:
    data = json.loads(API_FIXTURE.read_text(encoding="utf-8"))
    return data["reportTableData"]


def _document(body: str, title: str = "IPO GMP Live") -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body>{body}</body></html>"
    )


def variant_a(rows: list[dict]) -> str:
    """Baseline: a conventional table with thead/tbody and stable classes."""
    head = "".join(f'<th class="col-head">{html.escape(label)}</th>' for label, _ in COLUMNS)
    body = ""
    for row in rows:
        cells = "".join(f'<td class="cell">{_cell(row, key)}</td>' for _, key in COLUMNS)
        body += f'<tr class="ipo-row">{cells}</tr>'
    return _document(
        '<div class="container"><table class="ipo-table" id="reportTable">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def variant_b(rows: list[dict]) -> str:
    """Every class/id renamed - selector-based parsers break here."""
    doc = variant_a(rows)
    replacements = {
        'class="container"': 'class="x9f2-shell"',
        'class="ipo-table" id="reportTable"': 'class="dt-grid--v3" id="grid_88213"',
        'class="col-head"': 'class="dt-h"',
        'class="ipo-row"': 'class="dt-r"',
        'class="cell"': 'class="dt-c"',
    }
    for old, new in replacements.items():
        doc = doc.replace(old, new)
    return doc


def variant_c(rows: list[dict]) -> str:
    """Extra wrapper elements nested around and inside the table."""
    head = "".join(
        f"<th><div class='w1'><span class='w2'>{html.escape(label)}</span></div></th>"
        for label, _ in COLUMNS
    )
    body = ""
    for row in rows:
        cells = "".join(
            f"<td><div class='inner'><span class='v'>{_cell(row, key)}</span></div></td>"
            for _, key in COLUMNS
        )
        body += f"<tr>{cells}</tr>"
    return _document(
        "<div class='page'><section><div class='panel'><div class='panel-body'>"
        "<div class='table-responsive'><table><thead><tr>"
        f"{head}</tr></thead><tbody>{body}</tbody></table>"
        "</div></div></div></section></div>"
    )


def variant_d(rows: list[dict]) -> str:
    """Columns shuffled - position-based parsers break here."""
    shuffled = COLUMNS[:]
    random.Random(20260901).shuffle(shuffled)
    head = "".join(f"<th>{html.escape(label)}</th>" for label, _ in shuffled)
    body = ""
    for row in rows:
        cells = "".join(f"<td>{_cell(row, key)}</td>" for _, key in shuffled)
        body += f"<tr>{cells}</tr>"
    return _document(
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def variant_e(rows: list[dict]) -> str:
    """An expected column (GMP %) removed entirely."""
    columns = [pair for pair in COLUMNS if pair[0] != "GMP %"]
    head = "".join(f"<th>{html.escape(label)}</th>" for label, _ in columns)
    body = ""
    for row in rows:
        cells = "".join(f"<td>{_cell(row, key)}</td>" for _, key in columns)
        body += f"<tr>{cells}</tr>"
    return _document(
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def variant_f(_rows: list[dict]) -> str:
    """Structurally unrecognisable - the parser must fail safely, not guess."""
    return _document(
        "<div><p>Our IPO data is temporarily unavailable.</p>"
        "<p>Please check back shortly.</p>"
        "<table><thead><tr><th>Notice</th></tr></thead>"
        "<tbody><tr><td>No data available</td></tr></tbody></table></div>",
        title="Service unavailable",
    )


VARIANTS = {
    "variant_a_baseline.html": variant_a,
    "variant_b_renamed_classes.html": variant_b,
    "variant_c_extra_wrappers.html": variant_c,
    "variant_d_reordered_columns.html": variant_d,
    "variant_e_missing_column.html": variant_e,
    "variant_f_unrecognisable.html": variant_f,
}


def main() -> None:
    rows = _rows()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for filename, builder in VARIANTS.items():
        path = FIXTURES / filename
        path.write_text(builder(rows), encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
