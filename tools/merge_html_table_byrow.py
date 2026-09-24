# -*- coding: utf-8 -*-
"""
Merge the single <table> found in each matching *.html report into one big
table, stacking rows on top of each other (row-wise merge).

Each source file contributes its body rows to the merged table; a "Source"
column is prepended to every row so you can still tell which file a row came
from after the merge. The header (thead / first row of <th>) is taken from
the first file that has one.

Discovery:
    - Recursively walks --dir (default: this script's own folder) for *.html
      files.
    - If a positional filter is given, only files whose file name (not the
      containing folder name) contains that substring (case-insensitive) are
      kept. No filter means all *.html files are used.

Files are merged in discovery order. The output page itself has a "Source
order" panel with up/down buttons per file - reordering there moves that
file's whole block of rows within the table, live, in the browser.

Usage:
    python merge_html_table_byrow.py
    python merge_html_table_byrow.py problem
    python merge_html_table_byrow.py problem --dir D:\\graphiteperf\\data-summary
    python merge_html_table_byrow.py problem --dir D:\\graphiteperf\\data-summary --out out.html
"""

import argparse
import sys
from html import escape
from pathlib import Path

from bs4 import BeautifulSoup

STYLE = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; padding: 20px; color: #333; }
.container { max-width: 1600px; margin: 0 auto; }
h1 { margin-bottom: 10px; color: #1a1a2e; }
p { margin: 4px 0; color: #555; }
ol { margin: 8px 0 16px 24px; }
table { border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin: 10px 0; font-size: 13px; }
th, td { padding: 6px 10px; border-bottom: 1px solid #eee; white-space: nowrap; }
th { background: #1a1a2e; color: #fff; text-align: left; }
tr:hover { background: #f8f9fa; }
#orderPanel { background: #fff; border-radius: 8px; padding: 12px 16px; margin: 10px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 600px; }
#orderPanel h3 { margin-bottom: 6px; color: #16213e; }
#orderList { list-style: none; margin: 0; padding: 0; }
#orderList li { display: flex; align-items: center; gap: 8px; padding: 4px 0; border-bottom: 1px solid #f0f0f0; }
#orderList li .label { flex: 1; font-family: monospace; }
#orderList li .swatch { display: inline-block; width: 14px; height: 14px; border-radius: 3px; flex-shrink: 0; }
#orderList button { cursor: pointer; border: 1px solid #ddd; background: #f5f5f5; border-radius: 4px; padding: 2px 8px; }
#orderList button:hover { background: #e8e8e8; }
""".strip()

SCRIPT_TEMPLATE = """
var order = %(order)s;

function render() {
  var tbody = document.querySelector('#mainTable tbody');
  var rowsBySrc = {};
  tbody.querySelectorAll('tr').forEach(function (tr) {
    var s = tr.dataset.src;
    (rowsBySrc[s] = rowsBySrc[s] || []).push(tr);
  });
  order.forEach(function (s) {
    (rowsBySrc[s] || []).forEach(function (tr) { tbody.appendChild(tr); });
  });

  var list = document.getElementById('orderList');
  var itemsBySrc = {};
  list.querySelectorAll('li').forEach(function (li) { itemsBySrc[li.dataset.src] = li; });
  order.forEach(function (s) { list.appendChild(itemsBySrc[s]); });
}

function moveGroup(idx, dir) {
  var pos = order.indexOf(idx);
  var newPos = pos + dir;
  if (newPos < 0 || newPos >= order.length) return;
  var tmp = order[pos];
  order[pos] = order[newPos];
  order[newPos] = tmp;
  render();
}
""".strip()


def find_html_files(base_dir, filter_str, out_path):
    out_resolved = out_path.resolve()
    files = []
    for p in sorted(base_dir.rglob("*.html")):
        if p.resolve() == out_resolved:
            continue
        if filter_str and filter_str.lower() not in p.name.lower():
            continue
        files.append(p)
    return files


def split_rows(table):
    """Return (header_rows, body_rows), both lists of <tr> tags."""
    thead = table.find("thead", recursive=False)
    tbody = table.find("tbody", recursive=False)

    header_rows = thead.find_all("tr", recursive=False) if thead is not None else []

    if tbody is not None:
        body_rows = tbody.find_all("tr", recursive=False)
    else:
        all_rows = table.find_all("tr", recursive=False)
        if not header_rows and all_rows and all_rows[0].find("th"):
            header_rows, body_rows = [all_rows[0]], all_rows[1:]
        else:
            body_rows = all_rows
    return header_rows, body_rows


def build_source_colors(n):
    """n distinct pastel row colors, hues chosen to avoid the red band."""
    if n <= 0:
        return []
    if n == 1:
        return ["hsl(210, 65%, 88%)"]
    start, end = 35, 300  # skip 300-35 (red/pink/orange-red) so no color reads as "regress"
    return ["hsl(%.0f, 65%%, 88%%)" % (start + (end - start) * i / (n - 1)) for i in range(n)]


def build_merged_document(files, base_dir, filter_str):
    header_html = None
    body_rows_html = []
    labels = []
    n_rows = 0

    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(text, "html.parser")
        table = soup.find("table")
        if table is None:
            print("  [warn] no <table> in %s, skipped" % f.relative_to(base_dir))
            continue

        header_rows, body_rows = split_rows(table)
        label = f.stem
        src_idx = len(labels)
        labels.append(label)

        if header_html is None and header_rows:
            first_header_row = header_rows[0]
            src_th = soup.new_tag("th")
            src_th.string = "Source"
            if len(header_rows) > 1:
                src_th["rowspan"] = str(len(header_rows))
            first_header_row.insert(0, src_th)
            header_html = "".join(str(r) for r in header_rows)

        for row in body_rows:
            src_td = soup.new_tag("td")
            src_td.string = label
            row.insert(0, src_td)
            row["data-src"] = str(src_idx)
            row["class"] = row.get("class", []) + ["src-%d" % src_idx]
            body_rows_html.append(str(row))
            n_rows += 1

    n_tables = len(labels)
    colors = build_source_colors(n_tables)
    color_css = "\n".join(
        "tr.src-%d { background: %s; }" % (i, color) for i, color in enumerate(colors)
    )

    header_block = "<thead>\n%s\n</thead>\n" % header_html if header_html else ""
    table_html = '<table id="mainTable">\n%s<tbody>\n%s\n</tbody>\n</table>' % (
        header_block,
        "\n".join(body_rows_html),
    )

    order_items = "".join(
        '<li data-src="%d">'
        '<span class="swatch" style="background:%s"></span>'
        '<span class="label">%s</span>'
        '<button onclick="moveGroup(%d,-1)">▲</button>'
        '<button onclick="moveGroup(%d,1)">▼</button></li>'
        % (i, colors[i], escape(label), i, i)
        for i, label in enumerate(labels)
    )
    order_panel = (
        '<div id="orderPanel">\n<h3>Source order (adjust here)</h3>\n'
        '<ol id="orderList">%s</ol>\n</div>' % order_items
    )
    script = "<script>\n%s\n</script>" % (SCRIPT_TEMPLATE % {"order": list(range(len(labels)))})

    title = "Merged table" + (" - %s" % escape(filter_str) if filter_str else "")
    doc = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="UTF-8">\n'
        "<title>%s</title>\n<style>\n%s\n%s\n</style>\n</head>\n<body>\n"
        '<div class="container">\n'
        "<h1>%s</h1>\n"
        "<p>Base dir: %s</p>\n"
        "<p>Merged from %d file(s), %d row(s).</p>\n"
        "%s\n"
        "%s\n"
        "%s\n"
        "</div>\n</body>\n</html>\n"
    ) % (
        title,
        STYLE,
        color_css,
        title,
        escape(str(base_dir)),
        n_tables,
        n_rows,
        order_panel,
        table_html,
        script,
    )

    return doc, n_tables, n_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "filter",
        nargs="?",
        default=None,
        help="only include html files whose file name contains this substring (case-insensitive); omit for all files",
    )
    parser.add_argument("--dir", "-d", default=None, help="folder to search (default: this script's folder)")
    parser.add_argument("--out", "-o", default=None, help="output html path (default: <dir>/merged_<filter or all>.html)")
    args = parser.parse_args()

    base_dir = Path(args.dir).resolve() if args.dir else Path(__file__).resolve().parent
    if not base_dir.is_dir():
        print("[error] not a directory: %s" % base_dir)
        sys.exit(1)

    out_path = Path(args.out).resolve() if args.out else base_dir / ("merged_%s.html" % (args.filter or "all"))

    files = find_html_files(base_dir, args.filter, out_path)
    if not files:
        print("No matching html files found under %s (filter=%s)." % (base_dir, args.filter))
        sys.exit(1)

    print("Base dir: %s" % base_dir)
    print("Filter: %s" % (args.filter or "(none, all files)"))
    print("Found %d html file(s):" % len(files))
    for f in files:
        print("  %s" % f.relative_to(base_dir))

    doc, n_tables, n_rows = build_merged_document(files, base_dir, args.filter)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    print("\nMerged %d table(s), %d row(s) -> %s" % (n_tables, n_rows, out_path))


if __name__ == "__main__":
    main()
