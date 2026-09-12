"""Analyze Graphite vs Ganesh 'display_draw_to_swap' UMA metric for ONE run.

Metric: uma_metrics::compositing:display_draw_to_swap, computed the same way
as tools/perf/core/tbmv3/metrics/uma_metrics.sql does it:

    SELECT sample / 1000.0 FROM histogram_samples
    WHERE histogram_name = 'Compositing.Display.DrawToSwapUs'

i.e. every HistogramSample trace event named Compositing.Display.DrawToSwapUs,
converted from microseconds to milliseconds. This script reads the raw samples
directly out of each story's trace/traceEvents/*.json(.gz) file -- no
trace_processor / vpython dependency needed.

This script processes exactly ONE results folder per invocation (e.g.
D:\\graphiteperf\\0909-wpr\\run-20260909_093510). Aggregating multiple runs
side by side is intentionally left to a separate script.

Usage:
  python3 analyze_uma_results.py <results_dir>
  python3 analyze_uma_results.py <results_dir> --csv out.csv --html out.html
  python3 analyze_uma_results.py <results_dir> --threshold 5
"""

import argparse
import csv
import glob
import gzip
import os
import re
import sys
import time


HISTOGRAM_NAME = 'Compositing.Display.DrawToSwapUs'
METRIC_NAME = 'display_draw_to_swap'

_SAMPLE_RE = re.compile(
    r'"name":"' + re.escape(HISTOGRAM_NAME) + r'","name_hash":\d+,"name_iid":\d+,"sample":(\d+)'
)


def extract_draw_to_swap_ms(story_dir):
    """Return the list of display_draw_to_swap sample values (ms) for a story dir."""
    trace_patterns = [
        os.path.join(story_dir, 'artifacts', '*', '*', 'trace', 'traceEvents', '*.json.gz'),
        os.path.join(story_dir, 'artifacts', '*', '*', 'trace', 'traceEvents', '*.json'),
    ]
    trace_files = []
    for pattern in trace_patterns:
        trace_files.extend(glob.glob(pattern))

    samples_us = []
    for trace_file in trace_files:
        try:
            if trace_file.endswith('.gz'):
                with gzip.open(trace_file, 'rt', encoding='utf-8', errors='replace') as f:
                    content = f.read()
            else:
                with open(trace_file, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
        except Exception:
            continue

        for m in _SAMPLE_RE.finditer(content):
            samples_us.append(int(m.group(1)))

    return [s / 1000.0 for s in samples_us]


def _mean(values):
    return sum(values) / len(values) if values else None


def _median(values):
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def collect(results_dir):
    """Collect display_draw_to_swap samples for every story in results_dir.

    Returns: dict[story] -> {'graphite': [ms...], 'ganesh': [ms...]}
    """
    dirs_to_process = []
    for entry in sorted(os.listdir(results_dir)):
        entry_path = os.path.join(results_dir, entry)
        if not os.path.isdir(entry_path):
            continue
        if entry.endswith('_graphite') or entry.endswith('_ganesh'):
            dirs_to_process.append(entry)

    total = len(dirs_to_process)
    if total == 0:
        return {}

    print(f'Extracting {HISTOGRAM_NAME} from {total} story dirs...')
    t0 = time.time()

    results = {}
    for i, entry in enumerate(dirs_to_process, 1):
        entry_path = os.path.join(results_dir, entry)
        if entry.endswith('_graphite'):
            story = entry[:-len('_graphite')]
            label = 'graphite'
        else:
            story = entry[:-len('_ganesh')]
            label = 'ganesh'

        samples_ms = extract_draw_to_swap_ms(entry_path)
        results.setdefault(story, {})[label] = samples_ms

        if i % 20 == 0 or i == total:
            elapsed = time.time() - t0
            eta = (elapsed / i) * (total - i) if i < total else 0
            print(f'  {i}/{total} done ({elapsed:.1f}s elapsed, ~{eta:.0f}s remaining)')

    return results


def build_comparisons(results, threshold):
    """Build per-story comparison rows.

    Returns list of dicts with keys:
      story, g_mean, n_mean, g_median, n_median, g_count, n_count,
      diff_pct, status
    Rows where either side has zero samples get diff_pct=None, status='NA'.
    """
    rows = []
    for story in sorted(results.keys()):
        data = results[story]
        g = data.get('graphite', [])
        n = data.get('ganesh', [])

        g_mean, n_mean = _mean(g), _mean(n)
        g_median, n_median = _median(g), _median(n)

        if g_mean is None or n_mean is None:
            rows.append({
                'story': story, 'g_mean': g_mean, 'n_mean': n_mean,
                'g_median': g_median, 'n_median': n_median,
                'g_count': len(g), 'n_count': len(n),
                'diff_pct': None, 'status': 'NA',
            })
            continue

        if n_mean != 0:
            diff_pct = (g_mean - n_mean) / abs(n_mean) * 100
        else:
            diff_pct = 0.0

        # smallerIsBetter: graphite worse (regression) when its draw-to-swap
        # time is higher than ganesh's.
        if diff_pct >= threshold:
            status = 'REGRESS'
        elif diff_pct <= -threshold:
            status = 'IMPROVE'
        else:
            status = 'neutral'

        rows.append({
            'story': story, 'g_mean': g_mean, 'n_mean': n_mean,
            'g_median': g_median, 'n_median': n_median,
            'g_count': len(g), 'n_count': len(n),
            'diff_pct': diff_pct, 'status': status,
        })

    return rows


def print_report(rows, threshold, results_dir):
    print()
    print('=' * 100)
    print(f'GRAPHITE vs GANESH - {METRIC_NAME} ({HISTOGRAM_NAME})  [{results_dir}]')
    print('=' * 100)

    header = (f'{"Story":<45} {"Graphite(ms)":>13} {"Ganesh(ms)":>13} {"Diff%":>8} '
              f'{"Status":<9} {"G_med":>8} {"N_med":>8} {"G_n":>6} {"N_n":>6}')
    print(header)
    print('-' * len(header))

    regressions = improvements = neutral = na = 0
    for r in rows:
        if r['status'] == 'NA':
            na += 1
            print(f'{r["story"][:44]:<45} {"NA":>13} {"NA":>13} {"NA":>8} '
                  f'{"NA":<9} {"NA":>8} {"NA":>8} {r["g_count"]:>6} {r["n_count"]:>6}')
            continue
        if r['status'] == 'REGRESS':
            regressions += 1
        elif r['status'] == 'IMPROVE':
            improvements += 1
        else:
            neutral += 1
        print(f'{r["story"][:44]:<45} {r["g_mean"]:>13.2f} {r["n_mean"]:>13.2f} '
              f'{r["diff_pct"]:>+7.1f}% {r["status"]:<9} {r["g_median"]:>8.2f} '
              f'{r["n_median"]:>8.2f} {r["g_count"]:>6} {r["n_count"]:>6}')

    total = regressions + improvements + neutral
    print()
    print('=' * 100)
    print('SUMMARY')
    print('=' * 100)
    print(f'  Stories compared: {total} (+ {na} with missing data on at least one side)')
    if total:
        print(f'  Regressions (Graphite worse) >= {threshold}%: {regressions} ({100*regressions/total:.1f}%)')
        print(f'  Improvements (Graphite better) >= {threshold}%: {improvements} ({100*improvements/total:.1f}%)')
        print(f'  Neutral (within {threshold}%): {neutral} ({100*neutral/total:.1f}%)')


def write_csv(rows, csv_path):
    def _num(v):
        return '' if v is None else f'{v:.3f}'

    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Story', 'Graphite_Mean_ms', 'Ganesh_Mean_ms', 'Diff%', 'Status',
                          'Graphite_Median_ms', 'Ganesh_Median_ms',
                          'Graphite_Count', 'Ganesh_Count'])
        for r in rows:
            writer.writerow([
                r['story'], _num(r['g_mean']), _num(r['n_mean']), _num(r['diff_pct']),
                r['status'], _num(r['g_median']), _num(r['n_median']),
                r['g_count'], r['n_count'],
            ])
    print(f'\nCSV saved to: {csv_path}')


def write_html(rows, html_path, threshold, results_dir, info_text):
    total = sum(1 for r in rows if r['status'] != 'NA')
    regressions = sum(1 for r in rows if r['status'] == 'REGRESS')
    improvements = sum(1 for r in rows if r['status'] == 'IMPROVE')
    neutral = sum(1 for r in rows if r['status'] == 'neutral')
    na = sum(1 for r in rows if r['status'] == 'NA')

    def _fmt(v, prec=2):
        return 'NA' if v is None else f'{v:.{prec}f}'

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Graphite vs Ganesh - {METRIC_NAME}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; padding: 20px; color: #333; }}
.container {{ max-width: 1200px; margin: 0 auto; }}
h1 {{ margin-bottom: 10px; color: #1a1a2e; }}
h2 {{ margin: 20px 0 10px; color: #16213e; border-bottom: 2px solid #0f3460; padding-bottom: 5px; }}
.info-box {{ background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 15px; margin: 15px 0; white-space: pre-line; font-family: monospace; font-size: 13px; }}
.summary-cards {{ display: flex; gap: 15px; flex-wrap: wrap; margin: 15px 0; }}
.card {{ background: #fff; border-radius: 8px; padding: 20px; flex: 1; min-width: 180px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); text-align: center; }}
.card .number {{ font-size: 2em; font-weight: bold; }}
.card.total .number {{ color: #333; }}
.card.regress .number {{ color: #e74c3c; }}
.card.improve .number {{ color: #27ae60; }}
.card.neutral .number {{ color: #7f8c8d; }}
.card.na .number {{ color: #999; }}
.card .label {{ color: #666; margin-top: 5px; }}
.filters {{ background: #fff; border-radius: 8px; padding: 15px; margin: 15px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
.filters label {{ margin-right: 15px; }}
.filters input, .filters select {{ padding: 5px 10px; border: 1px solid #ddd; border-radius: 4px; margin-right: 10px; }}
table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin: 10px 0; font-size: 13px; }}
th {{ background: #1a1a2e; color: #fff; padding: 10px 8px; text-align: left; cursor: pointer; user-select: none; }}
th:hover {{ background: #16213e; }}
td {{ padding: 8px; border-bottom: 1px solid #eee; }}
tr:hover {{ background: #f8f9fa; }}
tr.regress {{ background: #fdf2f2; }}
tr.regress:hover {{ background: #fce4e4; }}
tr.improve {{ background: #f0fdf4; }}
tr.improve:hover {{ background: #dcfce7; }}
tr.na {{ color: #999; }}
.status {{ font-weight: bold; padding: 2px 8px; border-radius: 4px; font-size: 11px; }}
.status.REGRESS {{ background: #fee2e2; color: #dc2626; }}
.status.IMPROVE {{ background: #dcfce7; color: #16a34a; }}
.status.neutral {{ background: #f3f4f6; color: #6b7280; }}
.status.NA {{ background: #f3f4f6; color: #999; }}
#search {{ width: 300px; padding: 8px 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; }}
</style>
</head>
<body>
<div class="container">
<h1>Graphite vs Ganesh - {METRIC_NAME}</h1>
<div class="info-box">Metric: {HISTOGRAM_NAME} (histogram samples, converted us -&gt; ms)
Source: {results_dir}
{info_text}</div>

<h2>Summary</h2>
<div class="summary-cards">
  <div class="card total"><div class="number">{total}</div><div class="label">Stories compared</div></div>
  <div class="card regress"><div class="number">{regressions}</div><div class="label">Regressions ({100*regressions/total:.1f}% of compared)</div></div>
  <div class="card improve"><div class="number">{improvements}</div><div class="label">Improvements ({100*improvements/total:.1f}% of compared)</div></div>
  <div class="card neutral"><div class="number">{neutral}</div><div class="label">Neutral &lt;{threshold}%</div></div>
  <div class="card na"><div class="number">{na}</div><div class="label">Missing data (NA)</div></div>
</div>

<div class="filters">
  <label>Search: <input type="text" id="search" placeholder="Filter by story..." oninput="filterTable()"></label>
  <label>Status: <select id="statusFilter" onchange="filterTable()">
    <option value="all">All</option>
    <option value="REGRESS">Regressions only</option>
    <option value="IMPROVE">Improvements only</option>
    <option value="neutral">Neutral only</option>
    <option value="NA">NA only</option>
  </select></label>
  <span id="filterCount" style="margin-left:10px;font-weight:bold;color:#555;"></span>
</div>

<table id="mainTable">
<thead>
<tr>
<th onclick="sortTable(0)">Story</th>
<th onclick="sortTable(1)">Graphite Mean (ms)</th>
<th onclick="sortTable(2)">Ganesh Mean (ms)</th>
<th onclick="sortTable(3)">Diff%</th>
<th onclick="sortTable(4)">Status</th>
<th onclick="sortTable(5)">Graphite Median (ms)</th>
<th onclick="sortTable(6)">Ganesh Median (ms)</th>
<th onclick="sortTable(7)">Graphite N</th>
<th onclick="sortTable(8)">Ganesh N</th>
</tr>
</thead>
<tbody>
'''

    for r in rows:
        row_class = {'REGRESS': 'regress', 'IMPROVE': 'improve', 'NA': 'na'}.get(r['status'], '')
        diff_str = '' if r['diff_pct'] is None else f'{r["diff_pct"]:+.1f}%'
        html += (f'<tr class="{row_class}" data-status="{r["status"]}">'
                 f'<td>{r["story"]}</td>'
                 f'<td style="text-align:right">{_fmt(r["g_mean"])}</td>'
                 f'<td style="text-align:right">{_fmt(r["n_mean"])}</td>'
                 f'<td style="text-align:right">{diff_str}</td>'
                 f'<td><span class="status {r["status"]}">{r["status"]}</span></td>'
                 f'<td style="text-align:right">{_fmt(r["g_median"])}</td>'
                 f'<td style="text-align:right">{_fmt(r["n_median"])}</td>'
                 f'<td style="text-align:right">{r["g_count"]}</td>'
                 f'<td style="text-align:right">{r["n_count"]}</td>'
                 f'</tr>\n')

    html += '''</tbody></table>
</div>
<script>
function filterTable() {
  const search = document.getElementById('search').value.toLowerCase();
  const status = document.getElementById('statusFilter').value;
  const rows = document.querySelectorAll('#mainTable tbody tr');
  let visible = 0;
  rows.forEach(row => {
    const text = row.textContent.toLowerCase();
    const rowStatus = row.getAttribute('data-status');
    const matchSearch = !search || text.includes(search);
    const matchStatus = status === 'all' || rowStatus === status;
    const show = matchSearch && matchStatus;
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  document.getElementById('filterCount').textContent = visible + ' / ' + rows.length + ' items';
}

function sortTable(colIdx) {
  const table = document.getElementById('mainTable');
  const tbody = table.querySelector('tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  const dir = table.getAttribute('data-sort-dir') === 'asc' ? 'desc' : 'asc';
  table.setAttribute('data-sort-dir', dir);

  rows.sort((a, b) => {
    let aVal = a.cells[colIdx].textContent.trim();
    let bVal = b.cells[colIdx].textContent.trim();
    const aNum = parseFloat(aVal.replace('%', '').replace('+', ''));
    const bNum = parseFloat(bVal.replace('%', '').replace('+', ''));
    if (!isNaN(aNum) && !isNaN(bNum)) {
      return dir === 'asc' ? aNum - bNum : bNum - aNum;
    }
    return dir === 'asc' ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
  });
  rows.forEach(row => tbody.appendChild(row));
}
</script>
</body>
</html>'''

    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'HTML report saved to: {html_path}')


def main():
    parser = argparse.ArgumentParser(
        description='Compare Graphite vs Ganesh display_draw_to_swap for one results folder')
    parser.add_argument('results_dir', help='Path to a single run folder, e.g. '
                         'D:\\graphiteperf\\0909-wpr\\run-20260909_093510')
    parser.add_argument('--csv', help='Output CSV path (default: <results_dir>/display_draw_to_swap.csv)')
    parser.add_argument('--html', help='Output HTML path (default: <results_dir>/display_draw_to_swap.html)')
    parser.add_argument('--threshold', type=float, default=5.0,
                         help='Diff%% threshold for REGRESS/IMPROVE classification (default: 5)')
    args = parser.parse_args()

    if not os.path.isdir(args.results_dir):
        print(f'Error: {args.results_dir} is not a directory')
        sys.exit(1)

    results_dir = os.path.normpath(args.results_dir)
    csv_path = args.csv or os.path.join(results_dir, 'display_draw_to_swap.csv')
    html_path = args.html or os.path.join(results_dir, 'display_draw_to_swap.html')

    info_text = ''
    info_path = os.path.join(results_dir, 'info.txt')
    if os.path.isfile(info_path):
        with open(info_path, 'r', encoding='utf-8', errors='replace') as f:
            info_text = f.read()

    results = collect(results_dir)
    if not results:
        print(f'No graphite/ganesh story folders found in {results_dir}')
        sys.exit(1)

    rows = build_comparisons(results, args.threshold)
    print_report(rows, args.threshold, results_dir)
    write_csv(rows, csv_path)
    write_html(rows, html_path, args.threshold, results_dir, info_text)


if __name__ == '__main__':
    main()
