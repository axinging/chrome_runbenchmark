# -*- coding: utf-8 -*-
"""
Summarize per-subfolder HTML performance reports.

For every immediate subfolder (e.g. ptl, nv, uhd770) of the base directory,
this script collects the "All Comparisons" table from each *.html report,
keeps only the rows whose Metric column equals
`thread_total_rendering_cpu_time_per_frame` (i.e. the table you get after
applying that filter), and merges the per-file tables into a single summary
file named <subfolder>.html.

Tables inside a summary are ordered by the trailing number of the source file
name, e.g. run-20260710_144114-off.html -> 144114. There is one blank line
between consecutive tables.

Run it from the folder that contains the subfolders, or pass that folder as the
first command line argument.
"""

import os
import re
import sys
from pathlib import Path

METRIC = "thread_total_rendering_cpu_time_per_frame"

# ---------------------------------------------------------------------------
# Shared stylesheet (copied from the source reports so the summary looks alike)
# ---------------------------------------------------------------------------
STYLE = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; padding: 20px; color: #333; }
.container { max-width: 1400px; margin: 0 auto; }
h1 { margin-bottom: 10px; color: #1a1a2e; }
h2 { margin: 20px 0 10px; color: #16213e; border-bottom: 2px solid #0f3460; padding-bottom: 5px; }
table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin: 10px 0; font-size: 13px; }
th { background: #1a1a2e; color: #fff; padding: 10px 8px; text-align: left; }
td { padding: 8px; border-bottom: 1px solid #eee; }
tr:hover { background: #f8f9fa; }
tr.regress { background: #fdf2f2; }
tr.regress:hover { background: #fce4e4; }
tr.improve { background: #f0fdf4; }
tr.improve:hover { background: #dcfce7; }
.status { font-weight: bold; padding: 2px 8px; border-radius: 4px; font-size: 11px; }
.status.REGRESS { background: #fee2e2; color: #dc2626; }
.status.IMPROVE { background: #dcfce7; color: #16a34a; }
.status.neutral { background: #f3f4f6; color: #6b7280; }
""".strip()


def sort_key(path):
    """Trailing number of the file name, e.g. run-20260710_144114-off -> 144114."""
    nums = re.findall(r"\d+", path.stem)
    return int(nums[-1]) if nums else -1


def extract_main_table(html):
    """Return (thead_html, [row_html, ...]) for the `All Comparisons` table.

    Only rows whose Metric column (2nd <td>) equals METRIC are returned.
    """
    m = re.search(r'<table id="mainTable">(.*?)</table>', html, re.DOTALL)
    if not m:
        return None, []
    table = m.group(1)

    thead_m = re.search(r"<thead>.*?</thead>", table, re.DOTALL)
    thead = thead_m.group(0) if thead_m else ""

    rows = re.findall(r"<tr\b.*?</tr>", table, re.DOTALL)
    kept = []
    for row in rows:
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.DOTALL)
        if len(cells) >= 2:
            metric = re.sub(r"<[^>]+>", "", cells[1]).strip()
            if metric == METRIC:
                kept.append(row.strip())
    return thead, kept


def build_summary(subfolder):
    files = sorted(
        [p for p in subfolder.glob("*.html")], key=sort_key
    )
    if not files:
        return None, 0

    sections = []
    for f in files:
        html = f.read_text(encoding="utf-8", errors="replace")
        thead, rows = extract_main_table(html)
        if thead is None:
            print("    [warn] no mainTable in %s" % f.name)
            continue
        table_html = (
            '<table>\n%s\n<tbody>\n%s\n</tbody>\n</table>'
            % (thead, "\n".join(rows))
        )
        section = "<h2>%s</h2>\n%s" % (f.name, table_html)
        sections.append(section)

    # One blank line between consecutive tables/sections.
    body = "\n\n".join(sections)

    doc = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="UTF-8">\n'
        "<title>%s - thread_total_rendering_cpu_time_per_frame</title>\n"
        "<style>\n%s\n</style>\n</head>\n<body>\n"
        '<div class="container">\n'
        "<h1>%s - All Comparisons (filter: %s)</h1>\n"
        "%s\n"
        "</div>\n</body>\n</html>\n"
    ) % (subfolder.name, STYLE, subfolder.name, METRIC, body)

    return doc, len(sections)


def main():
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    base = base.resolve()

    subfolders = [p for p in sorted(base.iterdir()) if p.is_dir()]
    print("Base directory: %s" % base)
    print("Found %d subfolder(s): %s" % (len(subfolders), ", ".join(p.name for p in subfolders)))

    for sub in subfolders:
        doc, n = build_summary(sub)
        if doc is None:
            print("  %s: no html files, skipped" % sub.name)
            continue
        out = base / ("%s.html" % sub.name)
        out.write_text(doc, encoding="utf-8")
        print("  %s: merged %d table(s) -> %s" % (sub.name, n, out.name))


if __name__ == "__main__":
    main()
