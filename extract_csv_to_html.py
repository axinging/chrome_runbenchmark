#!/usr/bin/env python3
"""Extract selected columns/metrics from every CSV in the current directory and
merge them side by side into a single HTML report.

Layout: the key columns (Story, and Metric when shown) appear once, taken from
the first CSV; every CSV then contributes its own block of data columns plus the
computed diff column(s), appended to the right. Rows are joined on the key
columns, so a story missing from one CSV shows NA in that CSV's block.

Everything meant to be tweaked lives in the CONFIG block below; each setting can
also be overridden from the command line (see --help).
"""

# ============================================================================
# CONFIG - edit these
# ============================================================================

# Directory holding the CSV files. "." = the folder this script sits in.
# Only that folder is scanned (no recursion into sub-directories).
INPUT_DIR = "."

# Output HTML path. None -> "<input dir name>_extract.html" inside INPUT_DIR.
OUTPUT_HTML = None

# Columns to extract from each CSV, in output order.
COLUMNS = ["Story", "DF3s_G", "DF3s_N"]

# Columns used to join rows across CSVs. They are emitted only once (from the
# first CSV); every other column in COLUMNS is repeated per CSV.
KEY_COLUMNS = ["Story"]

# Metrics to keep. Use the string "ALL" (or ["ALL"]) for every metric.
# Rows whose Metric is not listed here are dropped.
METRICS = ["thread_total_rendering_cpu_time_per_frame"]

# Name of the column holding the metric identifier in the source CSVs.
METRIC_COLUMN = "Metric"

# Computed columns, repeated inside every CSV's block. Each entry is inserted
# right after `after`.
#   left/right : source column names
#   op         : "-", "+", "*", "/"
#   after      : column name to insert behind (None -> end of the block)
#   highlight  : "positive" (red when > 0), "negative" (red when < 0), or None
#   decimals   : rounding for the displayed value
# Set to [] to disable computed columns entirely.
COMPUTED_COLUMNS = [
    {
        "name": "DF3s_G-DF3s_N",
        "left": "DF3s_G",
        "right": "DF3s_N",
        "op": "-",
        "after": "DF3s_N",
        "highlight": "positive",
        "decimals": 2,
    },
]

# Show a "Metric" key column. "auto" -> only when more than one metric survives
# filtering. True / False force it on / off.
SHOW_METRIC_COLUMN = "auto"

# Row ordering: "story" sorts by the key columns; "input" keeps the order the
# rows appear in (first CSV first, then keys only seen in later CSVs).
SORT_BY = "story"

# Strings treated as missing data (case-insensitive). Missing -> "NA" output.
NA_VALUES = {"", "na", "n/a", "nan", "none", "null", "-"}

# Text used for missing values in the HTML.
NA_TEXT = "NA"

# CSS color applied to highlighted computed values.
HIGHLIGHT_COLOR = "#d00000"

# ============================================================================
# Implementation
# ============================================================================

import argparse
import csv
import html
import os
import sys
from datetime import datetime

OPS = {
    "-": lambda a, b: a - b,
    "+": lambda a, b: a + b,
    "*": lambda a, b: a * b,
    "/": lambda a, b: a / b if b else None,
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def is_na(value):
    return value is None or str(value).strip().lower() in NA_VALUES


def to_float(value):
    """Return float(value) or None when the cell is missing/non-numeric."""
    if is_na(value):
        return None
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return None


def wanted_metrics(metrics):
    """Normalize the METRICS setting; None means 'keep everything'."""
    if metrics is None:
        return None
    if isinstance(metrics, str):
        metrics = [metrics]
    if any(m.strip().upper() == "ALL" for m in metrics):
        return None
    return {m.strip() for m in metrics}


def build_block_columns(columns, key_columns, computed):
    """Per-CSV column block: non-key columns with computed columns interleaved."""
    block = [c for c in columns if c not in key_columns]
    for spec in computed:
        after = spec.get("after")
        if after and after in block:
            block.insert(block.index(after) + 1, spec["name"])
        else:
            block.append(spec["name"])
    return block


def read_csv_rows(path, columns, key_columns, metric_filter, computed):
    """Read one CSV -> (rows keyed by join key, ordered keys, missing columns).

    Each row dict holds the key columns plus the block columns (computed values
    are floats or None; raw values stay strings).
    """
    rows, order, dups = {}, [], 0
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        needed = set(columns)
        for spec in computed:
            needed.update((spec["left"], spec["right"]))
        missing = [c for c in sorted(needed) if c not in header]

        has_metric = METRIC_COLUMN in header
        for raw in reader:
            metric = (raw.get(METRIC_COLUMN) or "").strip() if has_metric else ""
            if metric_filter is not None and has_metric and metric not in metric_filter:
                continue

            row = {"__metric__": metric}
            for col in columns:
                row[col] = raw.get(col)
            for spec in computed:
                left = to_float(raw.get(spec["left"]))
                right = to_float(raw.get(spec["right"]))
                row[spec["name"]] = (None if left is None or right is None
                                     else OPS[spec.get("op", "-")](left, right))

            key = tuple((row.get(c) or "").strip() for c in key_columns) + (metric,)
            if key in rows:
                dups += 1
                continue
            rows[key] = row
            order.append(key)
    return rows, order, missing, dups


def cell_html(row, col, computed_by_name, block_start=False):
    """Render one <td>. row=None means this CSV has no data for the key."""
    spec = computed_by_name.get(col)
    value = None if row is None else row.get(col)
    classes = ["grp"] if block_start else []

    if spec is None:
        text = NA_TEXT if is_na(value) else str(value).strip()
    elif value is None:
        classes.append("num")
        text = NA_TEXT
    else:
        classes.append("num")
        text = "%.*f" % (spec.get("decimals", 2), value)
        mode = spec.get("highlight")
        if (mode == "positive" and value > 0) or (mode == "negative" and value < 0):
            classes.append("hot")

    attr = ' class="%s"' % " ".join(classes) if classes else ""
    return "<td%s>%s</td>" % (attr, html.escape(text))


def render_html(files, key_columns, block_columns, keys, computed, meta):
    """files: list of (label, rows dict). keys: ordered join keys."""
    computed_by_name = {spec["name"]: spec for spec in computed}
    numeric = set(computed_by_name)

    # Two header rows: CSV name spanning its block, then the block columns.
    head1 = "".join('<th rowspan="2">%s</th>' % html.escape(c) for c in key_columns)
    head1 += "".join('<th class="grp" colspan="%d">%s</th>'
                     % (len(block_columns), html.escape(label))
                     for label, _ in files)
    head2 = ""
    for _ in files:
        head2 += "".join(
            '<th class="%s">%s</th>'
            % (" ".join(x for x in (("num" if c in numeric else ""),
                                    ("grp" if i == 0 else "")) if x),
               html.escape(c))
            for i, c in enumerate(block_columns))

    body = []
    for key in keys:
        cells = ""
        first = next((rows[key] for _, rows in files if key in rows), None)
        for i, col in enumerate(key_columns):
            # Key values come from the first CSV that has this row.
            value = key[i] if i < len(key) else (first or {}).get(col)
            cells += "<td>%s</td>" % html.escape(
                NA_TEXT if is_na(value) else str(value).strip())
        for _, rows in files:
            row = rows.get(key)
            for i, col in enumerate(block_columns):
                cells += cell_html(row, col, computed_by_name, block_start=(i == 0))
        body.append("<tr>%s</tr>" % cells)

    meta_items = "".join("<li><b>%s:</b> %s</li>"
                         % (html.escape(k), html.escape(str(v)))
                         for k, v in meta.items())

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CSV extract</title>
<style>
  body {{ font-family: "Segoe UI", Arial, sans-serif; margin: 24px; color: #202124; }}
  h1 {{ font-size: 20px; margin: 0 0 8px; }}
  ul.meta {{ font-size: 12px; color: #5f6368; list-style: none; padding: 0;
             margin: 0 0 16px; }}
  ul.meta li {{ margin: 2px 0; }}
  table {{ border-collapse: collapse; font-size: 13px; }}
  th, td {{ border: 1px solid #dadce0; padding: 4px 10px; white-space: nowrap; }}
  th {{ background: #f1f3f4; text-align: left; }}
  thead th {{ position: sticky; top: 0; z-index: 2; }}
  thead tr:nth-child(2) th {{ top: 27px; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  th.grp, td.grp {{ border-left: 2px solid #9aa0a6; }}
  thead tr:first-child th.grp {{ text-align: center; }}
  tbody tr:nth-child(even) {{ background: #fafafa; }}
  td.hot {{ color: {highlight}; font-weight: 600; }}
</style>
</head>
<body>
<h1>CSV extract</h1>
<ul class="meta">{meta}</ul>
<table>
<thead><tr>{head1}</tr><tr>{head2}</tr></thead>
<tbody>
{body}
</tbody>
</table>
</body>
</html>
""".format(highlight=HIGHLIGHT_COLOR, meta=meta_items, head1=head1, head2=head2,
           body="\n".join(body))


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input_dir", nargs="?", default=INPUT_DIR,
                   help="directory containing the CSV files (default: script folder)")
    p.add_argument("-o", "--output", default=OUTPUT_HTML, help="output HTML path")
    p.add_argument("-c", "--columns",
                   help="comma-separated columns to extract (default: %s)"
                        % ",".join(COLUMNS))
    p.add_argument("-k", "--key-columns",
                   help="comma-separated join columns (default: %s)"
                        % ",".join(KEY_COLUMNS))
    p.add_argument("-m", "--metrics",
                   help="comma-separated metrics, or ALL (default: %s)"
                        % ",".join(METRICS if isinstance(METRICS, list) else [METRICS]))
    p.add_argument("--diff", action="append", metavar="LEFT-RIGHT",
                   help="computed difference, e.g. DF3s_G-DF3s_N; repeatable. "
                        "Overrides COMPUTED_COLUMNS.")
    p.add_argument("--no-diff", action="store_true", help="disable computed columns")
    p.add_argument("--sort-by", choices=["story", "input"], default=SORT_BY)
    return p.parse_args(argv)


def computed_from_cli(diffs):
    specs = []
    for item in diffs:
        if "-" not in item:
            raise SystemExit("--diff needs the form LEFT-RIGHT, got %r" % item)
        left, right = (part.strip() for part in item.split("-", 1))
        specs.append({"name": "%s-%s" % (left, right), "left": left, "right": right,
                      "op": "-", "after": right, "highlight": "positive",
                      "decimals": 2})
    return specs


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

    # "." (or a relative path) resolves against the script's own folder, so the
    # script works when copied next to the CSVs and run from anywhere.
    input_dir = (args.input_dir if os.path.isabs(args.input_dir)
                 else os.path.normpath(os.path.join(SCRIPT_DIR, args.input_dir)))
    if not os.path.isdir(input_dir):
        raise SystemExit("not a directory: %s" % input_dir)

    columns = ([c.strip() for c in args.columns.split(",") if c.strip()]
               if args.columns else list(COLUMNS))
    key_columns = ([c.strip() for c in args.key_columns.split(",") if c.strip()]
                   if args.key_columns else list(KEY_COLUMNS))
    key_columns = [c for c in key_columns if c in columns] or columns[:1]
    metric_filter = wanted_metrics(args.metrics.split(",") if args.metrics else METRICS)

    if args.no_diff:
        computed = []
    elif args.diff:
        computed = computed_from_cli(args.diff)
    else:
        computed = list(COMPUTED_COLUMNS)

    # Non-recursive: only *.csv files directly inside input_dir.
    csv_files = sorted(f for f in os.listdir(input_dir)
                       if f.lower().endswith(".csv")
                       and os.path.isfile(os.path.join(input_dir, f)))
    if not csv_files:
        raise SystemExit("no CSV files found in %s" % input_dir)

    files, keys, seen, metrics_seen = [], [], set(), set()
    for name in csv_files:
        rows, order, missing, dups = read_csv_rows(
            os.path.join(input_dir, name), columns, key_columns, metric_filter, computed)
        if missing:
            print("[warn] %s: missing column(s): %s" % (name, ", ".join(missing)))
        if dups:
            print("[warn] %s: %d duplicate key row(s) ignored" % (name, dups))
        for key in order:
            if key not in seen:
                seen.add(key)
                keys.append(key)
            metrics_seen.add(key[-1])
        files.append((os.path.splitext(name)[0], rows))
        print("[info] %s -> %d row(s)" % (name, len(rows)))

    if not keys:
        raise SystemExit("no rows matched the metric filter")

    show_metric = (len(metrics_seen - {""}) > 1 if SHOW_METRIC_COLUMN == "auto"
                   else bool(SHOW_METRIC_COLUMN))
    # The metric is always the last element of the join key; only display it when
    # more than one metric is in play (or when forced on).
    display_keys = list(key_columns) + ([METRIC_COLUMN] if show_metric else [])
    block_columns = build_block_columns(columns, key_columns, computed)

    if args.sort_by == "story":
        keys.sort(key=lambda k: tuple(str(x) for x in k))

    output = args.output or os.path.join(
        input_dir, "%s_extract.html" % os.path.basename(os.path.abspath(input_dir)))

    meta = {
        "Source directory": os.path.abspath(input_dir),
        "CSV files": "%d (%s)" % (len(csv_files), ", ".join(csv_files)),
        "Metrics": "ALL" if metric_filter is None else ", ".join(sorted(metric_filter)),
        "Key columns": ", ".join(display_keys),
        "Per-file columns": ", ".join(block_columns),
        "Rows": len(keys),
        "Generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    with open(output, "w", encoding="utf-8") as fh:
        fh.write(render_html(files, display_keys, block_columns, keys,
                             computed, meta))
    print("[done] %s" % os.path.abspath(output))


if __name__ == "__main__":
    main()
