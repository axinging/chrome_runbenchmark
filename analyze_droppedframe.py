"""Analyze Graphite vs Ganesh benchmark results.

Usage:
  python3 analyze_results.py <results_dir>
  python3 analyze_results.py <results_dir> --csv output.csv
  python3 analyze_results.py <results_dir> --metric frame_times
  python3 analyze_results.py <results_dir> --only-regressions
"""

import argparse
import csv
import glob
import gzip
import json
import os
import re
import sys
import time


def parse_histograms_from_html(html_path):
    """Extract histogram JSON objects from a results.html file."""
    with open(html_path, 'r', encoding='utf-8') as f:
        content = f.read()

    histograms = {}
    for line in content.split('\n'):
        line = line.strip()
        if line.startswith('{') and '"sampleValues"' in line and '"name"' in line:
            try:
                obj = json.loads(line)
                name = obj.get('name')
                if name:
                    histograms[name] = obj
            except json.JSONDecodeError:
                pass
    return histograms


def get_metric_value(histogram):
    """Get the mean value from a histogram entry."""
    running = histogram.get('running', [])
    if len(running) > 3:
        return running[3]  # mean
    samples = histogram.get('sampleValues', [])
    if samples:
        return sum(samples) / len(samples)
    return None


def get_metric_direction(histogram):
    """Return 'smaller' or 'bigger' based on unit string."""
    unit = histogram.get('unit', '')
    if 'smallerIsBetter' in unit:
        return 'smaller'
    elif 'biggerIsBetter' in unit:
        return 'bigger'
    return 'unknown'


def collect_results(results_dir):
    """Collect all results from the results directory.

    Returns: dict[story_name] -> {'graphite': {metric: histogram}, 'ganesh': {metric: histogram}}
    """
    results = {}

    for entry in os.listdir(results_dir):
        entry_path = os.path.join(results_dir, entry)
        if not os.path.isdir(entry_path):
            continue

        html_path = os.path.join(entry_path, 'results.html')
        if not os.path.isfile(html_path):
            continue

        # Parse folder name: <story_name>_<label>
        if entry.endswith('_graphite'):
            story = entry[:-len('_graphite')]
            label = 'graphite'
        elif entry.endswith('_ganesh'):
            story = entry[:-len('_ganesh')]
            label = 'ganesh'
        else:
            continue

        histograms = parse_histograms_from_html(html_path)
        if not histograms:
            continue

        if story not in results:
            results[story] = {}
        results[story][label] = histograms

    return results


def extract_fps_from_trace(story_dir):
    """Extract FPS and PercentDroppedFrames3/4 from trace files in a story result dir.

    Returns: dict with keys:
      - 'PercentDroppedFrames3': list of sample values (= AllSequences, the combined
        animations-union-interactions guiding metric)
      - 'PercentDroppedFrames4': list of sample values (= AllSequences)
      - 'PercentDroppedFrames{3,4}_AllAnimations':  per-suffix breakdown, 页面动画
      - 'PercentDroppedFrames{3,4}_AllInteractions': per-suffix breakdown, 滚动/缩放
      - 'PercentDroppedFrames{3,4}_AllSequences':    per-suffix breakdown, 两者并集
      - 'avg_fps': float or None (from DisplayRenderingStats frame boundaries)
      - 'frames': int or None
      - 'p50_frame_time_ms': float or None
      - 'p95_frame_time_ms': float or None
      - 'dropped_frames_pct': float or None
    """
    fps_data = {
        'PercentDroppedFrames3': [], 'PercentDroppedFrames4': [],
        # Per-suffix breakdown. AllSequences = AllAnimations ∪ AllInteractions,
        # so the combined keys above are derived from AllSequences (see end of fn)
        # rather than by merging all three (which would double-count animations).
        'PercentDroppedFrames3_AllAnimations': [],
        'PercentDroppedFrames3_AllInteractions': [],
        'PercentDroppedFrames3_AllSequences': [],
        'PercentDroppedFrames4_AllAnimations': [],
        'PercentDroppedFrames4_AllInteractions': [],
        'PercentDroppedFrames4_AllSequences': [],
        'avg_fps': None, 'frames': None,
        'p50_frame_time_ms': None, 'p95_frame_time_ms': None,
        'dropped_frames_pct': None,
    }

    # Find trace files (gzipped json in traceEvents/)
    trace_patterns = [
        os.path.join(story_dir, 'artifacts', '*', '*', 'trace', 'traceEvents', '*.json.gz'),
        os.path.join(story_dir, 'artifacts', '*', '*', 'trace', 'traceEvents', '*.json'),
    ]
    trace_files = []
    for pattern in trace_patterns:
        trace_files.extend(glob.glob(pattern))

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

        # Match HistogramSample events with Graphics.Smoothness.PercentDroppedFrames{3,4}.
        # Capture the aggregation suffix too: AllAnimations (页面动画) /
        # AllInteractions (滚动/缩放) / AllSequences (两者并集).
        for m in re.finditer(
            r'"name":"(Graphics\.Smoothness\.PercentDroppedFrames[34])'
            r'\.(AllAnimations|AllInteractions|AllSequences)",'
            r'"name_hash":\d+,"name_iid":\d+,"sample":(\d+)',
            content
        ):
            metric_name = m.group(1).replace('Graphics.Smoothness.', '')
            suffix = m.group(2)
            sample = int(m.group(3))
            key = f'{metric_name}_{suffix}'
            if key in fps_data:
                fps_data[key].append(sample)

        # Extract FPS from BenchmarkInstrumentation::DisplayRenderingStats frame boundaries
        if fps_data['avg_fps'] is None:
            timestamps = []
            # Use a simple pattern: find each occurrence then grab ts from same JSON object
            for m in re.finditer(
                r'"name"\s*:\s*"BenchmarkInstrumentation::DisplayRenderingStats"[^}\n]*"ts"\s*:\s*(\d+)',
                content
            ):
                timestamps.append(int(m.group(1)))
            # Also handle case where ts appears before name in the same object
            if not timestamps:
                for m in re.finditer(
                    r'"ts"\s*:\s*(\d+)[^}\n]*"name"\s*:\s*"BenchmarkInstrumentation::DisplayRenderingStats"',
                    content
                ):
                    timestamps.append(int(m.group(1)))

            if len(timestamps) >= 2:
                timestamps.sort()
                frame_times_ms = [
                    (timestamps[i + 1] - timestamps[i]) / 1000.0
                    for i in range(len(timestamps) - 1)
                ]
                avg_ft = sum(frame_times_ms) / len(frame_times_ms)
                sorted_ft = sorted(frame_times_ms)
                p50 = sorted_ft[len(sorted_ft) // 2]
                p95 = sorted_ft[int(len(sorted_ft) * 0.95)]
                threshold_drop = p50 * 1.5
                dropped = sum(1 for ft in frame_times_ms if ft > threshold_drop)

                fps_data['avg_fps'] = 1000.0 / avg_ft
                fps_data['frames'] = len(timestamps)
                fps_data['p50_frame_time_ms'] = p50
                fps_data['p95_frame_time_ms'] = p95
                fps_data['dropped_frames_pct'] = 100.0 * dropped / len(frame_times_ms)

    # Combined PercentDroppedFrames{3,4} = AllSequences (animations ∪ interactions,
    # the official guiding metric). Fall back to Animations / Interactions if a
    # trace happens to lack the Sequences aggregate.
    for n in ('3', '4'):
        combined = f'PercentDroppedFrames{n}'
        fps_data[combined] = (
            fps_data[f'{combined}_AllSequences']
            or fps_data[f'{combined}_AllAnimations']
            or fps_data[f'{combined}_AllInteractions']
        )

    return fps_data


def collect_fps_data(results_dir):
    """Collect FPS data for all stories.

    Returns: dict[story_name] -> {'graphite': fps_dict, 'ganesh': fps_dict}
    """
    fps_results = {}

    # Count total dirs first for progress
    dirs_to_process = []
    for entry in os.listdir(results_dir):
        entry_path = os.path.join(results_dir, entry)
        if not os.path.isdir(entry_path):
            continue
        if entry.endswith('_graphite') or entry.endswith('_ganesh'):
            dirs_to_process.append(entry)

    total = len(dirs_to_process)
    if total == 0:
        return fps_results

    print(f'[FPS] Extracting FPS & PercentDroppedFrames from {total} trace files...')
    t0 = time.time()

    for i, entry in enumerate(dirs_to_process, 1):
        entry_path = os.path.join(results_dir, entry)

        if entry.endswith('_graphite'):
            story = entry[:-len('_graphite')]
            label = 'graphite'
        else:
            story = entry[:-len('_ganesh')]
            label = 'ganesh'

        fps_data = extract_fps_from_trace(entry_path)
        if story not in fps_results:
            fps_results[story] = {}
        fps_results[story][label] = fps_data

        # Progress every 10 entries or at the end
        if i % 10 == 0 or i == total:
            elapsed = time.time() - t0
            eta = (elapsed / i) * (total - i) if i < total else 0
            print(f'[FPS] {i}/{total} done ({elapsed:.1f}s elapsed, ~{eta:.0f}s remaining)')

    return fps_results


# Key metrics to focus on for rendering performance comparison
KEY_METRICS = [
    'frame_times',
    'cpu_wall_time_ratio',
    'thread_total_all_cpu_time_per_frame',
    'thread_total_rendering_cpu_time_per_frame',
    'thread_GPU_cpu_time_per_frame',
    'thread_raster_cpu_time_per_frame',
    'thread_renderer_main_cpu_time_per_frame',
    'thread_renderer_compositor_cpu_time_per_frame',
    'thread_display_compositor_cpu_time_per_frame',
    'thread_browser_cpu_time_per_frame',
]


def analyze(results_dir, metric_filter=None, only_regressions=False,
            csv_output=None, html_output=None, threshold=5.0, fps=True, df=True):
    """Analyze and compare results."""
    results = collect_results(results_dir)
    fps_data = collect_fps_data(results_dir) if (fps or df) else {}

    if not results:
        print(f'No results found in {results_dir}')
        return

    # Read info.txt if available
    info_path = os.path.join(results_dir, 'info.txt')
    if os.path.isfile(info_path):
        print('=' * 80)
        print('SYSTEM INFO')
        print('=' * 80)
        with open(info_path, 'r') as f:
            print(f.read())

    # Determine which metrics to show
    if metric_filter:
        metrics_to_show = [m for m in KEY_METRICS if metric_filter in m]
        if not metrics_to_show:
            # Try exact match across all available metrics
            metrics_to_show = [metric_filter]
    else:
        metrics_to_show = KEY_METRICS

    # Collect comparison data
    comparisons = []  # (story, metric, graphite_val, ganesh_val, diff_pct, direction, is_regression)

    for story in sorted(results.keys()):
        story_data = results[story]
        if 'graphite' not in story_data or 'ganesh' not in story_data:
            continue

        graphite_histograms = story_data['graphite']
        ganesh_histograms = story_data['ganesh']

        # Collect all available metrics for this story
        all_metrics = set(graphite_histograms.keys()) | set(ganesh_histograms.keys())

        for metric in metrics_to_show:
            if metric not in all_metrics:
                continue

            g_hist = graphite_histograms.get(metric)
            n_hist = ganesh_histograms.get(metric)

            if not g_hist or not n_hist:
                continue

            g_val = get_metric_value(g_hist)
            n_val = get_metric_value(n_hist)

            if g_val is None or n_val is None:
                continue

            # Calculate difference percentage (graphite vs ganesh)
            if n_val != 0:
                diff_pct = ((g_val - n_val) / abs(n_val)) * 100
            else:
                diff_pct = 0.0

            direction = get_metric_direction(g_hist)

            # Determine if this is a regression for graphite
            if direction == 'smaller':
                is_regression = g_val > n_val  # Higher is worse
            elif direction == 'bigger':
                is_regression = g_val < n_val  # Lower is worse
            else:
                is_regression = False

            comparisons.append((story, metric, g_val, n_val, diff_pct, direction, is_regression))

    if only_regressions:
        comparisons = [(s, m, gv, nv, d, dir, r) for s, m, gv, nv, d, dir, r in comparisons
                       if r and abs(d) >= threshold]

    # Print results
    print('=' * 80)
    print(f'GRAPHITE vs GANESH COMPARISON ({len(results)} stories with both results)')
    print(f'Metrics: {", ".join(metrics_to_show)}')
    if only_regressions:
        print(f'Showing only regressions >= {threshold}%')
    print('=' * 80)
    print()

    if not comparisons:
        print('No comparison data found.')
        return

    # Build FPS lookup: story -> dict with fps + per-suffix dropped-frame values.
    #   df{3,4}_{seq,anim,int}_{g,n}
    #   seq  = AllSequences   (总, animations ∪ interactions)
    #   anim = AllAnimations  (页面动画)
    #   int  = AllInteractions(滚动 / 缩放等交互)
    def _fps_avg(samples):
        if not samples:
            return None
        return sum(samples) / len(samples)

    def _df(data, n, suffix):
        # 'AllSequences' uses the combined key (already = AllSequences, with fallback)
        if suffix == 'AllSequences':
            return _fps_avg(data.get(f'PercentDroppedFrames{n}', []))
        return _fps_avg(data.get(f'PercentDroppedFrames{n}_{suffix}', []))

    fps_lookup = {}
    if (fps or df) and fps_data:
        for story in fps_data:
            g_data = fps_data[story].get('graphite', {})
            n_data = fps_data[story].get('ganesh', {})
            fl = {
                'fps_g': g_data.get('avg_fps') if fps else None,
                'fps_n': n_data.get('avg_fps') if fps else None,
            }
            if df:
                for n in ('3', '4'):
                    for tag, suffix in (('seq', 'AllSequences'),
                                        ('anim', 'AllAnimations'),
                                        ('int', 'AllInteractions')):
                        fl[f'df{n}_{tag}_g'] = _df(g_data, n, suffix)
                        fl[f'df{n}_{tag}_n'] = _df(n_data, n, suffix)
            fps_lookup[story] = fl

    def _fmt_pct(v):
        return f'{v:.0f}%' if v is not None else 'NA'

    def _fmt_fps(v):
        return f'{v:.1f}' if v is not None else 'NA'

    # Print table
    show_extra = fps or df
    if show_extra:
        extra_hdrs = ''
        if fps:
            extra_hdrs += f' {"FPS_G":>6} {"FPS_N":>6}'
        if df:
            extra_hdrs += f' {"DF3_G":>6} {"DF3_N":>6} {"DF4_G":>6} {"DF4_N":>6}'
        header = (f'{"Story":<45} {"Metric":<45} {"Graphite":>10} {"Ganesh":>10} {"Diff%":>8} {"Status":<10}'
                  + extra_hdrs)
    else:
        header = f'{"Story":<45} {"Metric":<45} {"Graphite":>10} {"Ganesh":>10} {"Diff%":>8} {"Status":<10}'
    print(header)
    print('-' * len(header))

    regressions = 0
    improvements = 0

    for story, metric, g_val, n_val, diff_pct, direction, is_regression in comparisons:
        if is_regression:
            status = 'REGRESS'
            regressions += 1
        elif abs(diff_pct) >= threshold:
            status = 'IMPROVE'
            improvements += 1
        else:
            status = 'neutral'

        # Truncate long names
        story_short = story[:44] if len(story) > 44 else story
        metric_short = metric[:44] if len(metric) > 44 else metric

        line = f'{story_short:<45} {metric_short:<45} {g_val:>10.4f} {n_val:>10.4f} {diff_pct:>+7.1f}% {status:<10}'
        if show_extra:
            fl = fps_lookup.get(story, {})
            if fps:
                line += f' {_fmt_fps(fl.get("fps_g")):>6} {_fmt_fps(fl.get("fps_n")):>6}'
            if df:
                # Console stays compact: show combined (Seq); full per-suffix
                # breakdown is in the HTML report.
                line += (f' {_fmt_pct(fl.get("df3_seq_g")):>6} {_fmt_pct(fl.get("df3_seq_n")):>6}'
                         f' {_fmt_pct(fl.get("df4_seq_g")):>6} {_fmt_pct(fl.get("df4_seq_n")):>6}')
        print(line)

    # Summary
    print()
    print('=' * 80)
    print('SUMMARY')
    print('=' * 80)
    total = len(comparisons)
    neutral = total - regressions - improvements
    print(f'  Total comparisons: {total}')
    print(f'  Regressions (Graphite worse): {regressions} ({100*regressions/total:.1f}%)')
    print(f'  Improvements (Graphite better): {improvements} ({100*improvements/total:.1f}%)')
    print(f'  Neutral (within {threshold}%): {neutral} ({100*neutral/total:.1f}%)')

    # Per-metric summary
    print()
    print('Per-metric summary:')
    metric_stats = {}
    for story, metric, g_val, n_val, diff_pct, direction, is_regression in comparisons:
        if metric not in metric_stats:
            metric_stats[metric] = {'diffs': [], 'regressions': 0, 'improvements': 0}
        metric_stats[metric]['diffs'].append(diff_pct)
        if is_regression and abs(diff_pct) >= threshold:
            metric_stats[metric]['regressions'] += 1
        elif not is_regression and abs(diff_pct) >= threshold:
            metric_stats[metric]['improvements'] += 1

    for metric, stats in sorted(metric_stats.items()):
        diffs = stats['diffs']
        avg_diff = sum(diffs) / len(diffs) if diffs else 0
        print(f'  {metric}: avg diff={avg_diff:+.2f}%, '
              f'regressions={stats["regressions"]}, improvements={stats["improvements"]}, '
              f'count={len(diffs)}')

    # CSV output
    if csv_output:
        # Per-story FPS + per-suffix dropped-frame columns. These are per-story
        # values, so they repeat on every metric row of the same story.
        # DF{3,4}{s,a,i}: s=AllSequences(总) a=AllAnimations(动画) i=AllInteractions(交互)
        fps_hdr = ['FPS_G', 'FPS_N'] if fps else []
        df_hdr = []
        if df:
            for n in ('3', '4'):
                for tag in ('s', 'a', 'i'):
                    df_hdr += [f'DF{n}{tag}_G', f'DF{n}{tag}_N']

        def _csv_num(v):
            return '' if v is None else f'{v:.2f}'

        with open(csv_output, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Story', 'Metric', 'Unit', 'Graphite', 'Ganesh',
                            'Diff%', 'Direction', 'IsRegression'] + fps_hdr + df_hdr)
            for story, metric, g_val, n_val, diff_pct, direction, is_regression in comparisons:
                fl = fps_lookup.get(story, {})
                extra = []
                if fps:
                    extra += [_csv_num(fl.get('fps_g')), _csv_num(fl.get('fps_n'))]
                if df:
                    for n in ('3', '4'):
                        for tag in ('seq', 'anim', 'int'):
                            extra += [_csv_num(fl.get(f'df{n}_{tag}_g')),
                                      _csv_num(fl.get(f'df{n}_{tag}_n'))]
                writer.writerow([story, metric, direction, f'{g_val:.4f}', f'{n_val:.4f}',
                                f'{diff_pct:.2f}', direction, is_regression] + extra)
        print(f'\nCSV saved to: {csv_output}')

    # HTML output
    if html_output:
        generate_html_report(html_output, comparisons, metric_stats, info_path,
                             results, metrics_to_show, threshold,
                             regressions, improvements, total,
                             fps_lookup if (fps or df) else None,
                             show_fps=fps, show_df=df)


def generate_html_report(html_path, comparisons, metric_stats, info_path,
                         results, metrics_to_show, threshold,
                         regressions, improvements, total, fps_lookup=None,
                         show_fps=True, show_df=True):
    """Generate an interactive HTML report."""
    def _hpct(v):
        return f'{v:.0f}%' if v is not None else 'NA'

    def _hfps(v):
        return f'{v:.1f}' if v is not None else 'NA'

    # Read system info
    info_text = ''
    if os.path.isfile(info_path):
        with open(info_path, 'r') as f:
            info_text = f.read()

    # Build per-story summary
    story_summary = {}
    for story, metric, g_val, n_val, diff_pct, direction, is_regression in comparisons:
        if story not in story_summary:
            story_summary[story] = {'regressions': 0, 'improvements': 0, 'neutral': 0, 'rows': []}
        if is_regression and abs(diff_pct) >= threshold:
            story_summary[story]['regressions'] += 1
        elif not is_regression and abs(diff_pct) >= threshold:
            story_summary[story]['improvements'] += 1
        else:
            story_summary[story]['neutral'] += 1
        story_summary[story]['rows'].append((metric, g_val, n_val, diff_pct, direction, is_regression))

    neutral = total - regressions - improvements

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Graphite vs Ganesh - Performance Comparison</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; padding: 20px; color: #333; }}
.container {{ max-width: 1400px; margin: 0 auto; }}
h1 {{ margin-bottom: 10px; color: #1a1a2e; }}
h2 {{ margin: 20px 0 10px; color: #16213e; border-bottom: 2px solid #0f3460; padding-bottom: 5px; }}
h3 {{ margin: 15px 0 8px; color: #333; }}
.info-box {{ background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 15px; margin: 15px 0; white-space: pre-line; font-family: monospace; font-size: 13px; }}
.summary-cards {{ display: flex; gap: 15px; flex-wrap: wrap; margin: 15px 0; }}
.card {{ background: #fff; border-radius: 8px; padding: 20px; flex: 1; min-width: 200px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); text-align: center; }}
.card .number {{ font-size: 2em; font-weight: bold; }}
.card.total .number {{ color: #333; }}
.card.regress .number {{ color: #e74c3c; }}
.card.improve .number {{ color: #27ae60; }}
.card.neutral .number {{ color: #7f8c8d; }}
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
.status {{ font-weight: bold; padding: 2px 8px; border-radius: 4px; font-size: 11px; }}
.status.REGRESS {{ background: #fee2e2; color: #dc2626; }}
.status.IMPROVE {{ background: #dcfce7; color: #16a34a; }}
.status.neutral {{ background: #f3f4f6; color: #6b7280; }}
.metric-summary {{ margin: 10px 0; }}
.metric-summary table {{ font-size: 14px; }}
.bar {{ display: inline-block; height: 14px; border-radius: 3px; }}
.bar.pos {{ background: #ef4444; }}
.bar.neg {{ background: #22c55e; }}
.collapsible {{ cursor: pointer; padding: 10px; background: #fff; border: 1px solid #ddd; border-radius: 6px; margin: 5px 0; }}
.collapsible:hover {{ background: #f8f9fa; }}
.collapsible-content {{ display: none; padding: 0 10px; }}
.collapsible.active + .collapsible-content {{ display: block; }}
.story-header {{ display: flex; justify-content: space-between; align-items: center; }}
.badges {{ display: flex; gap: 5px; }}
.badge {{ padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: bold; }}
.badge.r {{ background: #fee2e2; color: #dc2626; }}
.badge.i {{ background: #dcfce7; color: #16a34a; }}
.badge.n {{ background: #f3f4f6; color: #6b7280; }}
#search {{ width: 300px; padding: 8px 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; }}
</style>
</head>
<body>
<div class="container">
<h1>Graphite vs Ganesh - Performance Comparison</h1>
<div class="info-box">{info_text}</div>

<h2>Summary</h2>
<div class="summary-cards">
  <div class="card total"><div class="number">{total}</div><div class="label">Total Comparisons</div></div>
  <div class="card regress"><div class="number">{regressions}</div><div class="label">Regressions ({100*regressions/total:.1f}%)</div></div>
  <div class="card improve"><div class="number">{improvements}</div><div class="label">Improvements ({100*improvements/total:.1f}%)</div></div>
  <div class="card neutral"><div class="number">{neutral}</div><div class="label">Neutral &lt;{threshold}% ({100*neutral/total:.1f}%)</div></div>
</div>

<h2>Per-Metric Summary</h2>
<table>
<tr><th>Metric</th><th>Avg Diff%</th><th>Regressions</th><th>Improvements</th><th>Count</th><th>Visual</th></tr>
'''

    for metric in sorted(metric_stats.keys()):
        stats = metric_stats[metric]
        diffs = stats['diffs']
        avg_diff = sum(diffs) / len(diffs) if diffs else 0
        bar_width = min(abs(avg_diff) * 2, 200)
        bar_class = 'pos' if avg_diff > 0 else 'neg'
        # For smallerIsBetter metrics, positive diff means regression
        html += f'''<tr>
  <td><b>{metric}</b></td>
  <td style="text-align:right">{avg_diff:+.2f}%</td>
  <td style="text-align:center;color:#dc2626">{stats["regressions"]}</td>
  <td style="text-align:center;color:#16a34a">{stats["improvements"]}</td>
  <td style="text-align:center">{len(diffs)}</td>
  <td><span class="bar {bar_class}" style="width:{bar_width}px"></span></td>
</tr>
'''
    html += '</table>\n'

    # Full detail table
    html += '\n<h2>All Comparisons</h2>\n'
    if show_df and fps_lookup:
        html += ('<p style="font-size:12px;color:#666;margin:5px 0 10px;">'
                 'DF3 / DF4 = PercentDroppedFrames v3 / v4（丢帧率 %，越低越流畅）。'
                 '后缀 <b>s</b>=AllSequences（总）、<b>a</b>=AllAnimations（页面动画）、'
                 '<b>i</b>=AllInteractions（滚动/缩放等交互）；<b>_G</b>=Graphite、<b>_N</b>=Ganesh；'
                 'NA = 该类别无数据（如纯动画页没有交互序列）。悬停表头可看完整名称。</p>\n')
    html += '''<div class="filters">
  <label>Search: <input type="text" id="search" placeholder="Filter by story or metric..." oninput="filterTable()"></label>
  <label>Status: <select id="statusFilter" onchange="filterTable()">
    <option value="all">All</option>
    <option value="REGRESS">Regressions only</option>
    <option value="IMPROVE">Improvements only</option>
    <option value="neutral">Neutral only</option>
  </select></label>
  <span id="filterCount" style="margin-left:10px;font-weight:bold;color:#555;"></span>
</div>
'''
    # Column model: 7 fixed columns + optional FPS / per-suffix DF columns.
    base_cols = ['Story', 'Metric', 'Graphite', 'Ganesh', 'Diff%', 'Direction', 'Status']
    extra_cols = []  # (label, title)
    if fps_lookup:
        if show_fps:
            extra_cols += [('FPS_G', 'FPS Graphite'), ('FPS_N', 'FPS Ganesh')]
        if show_df:
            for n in ('3', '4'):
                for tag, full in (('s', 'AllSequences'), ('a', 'AllAnimations'),
                                  ('i', 'AllInteractions')):
                    extra_cols += [(f'DF{n}{tag}_G', f'DF{n} {full} Graphite'),
                                   (f'DF{n}{tag}_N', f'DF{n} {full} Ganesh')]
    all_labels = base_cols + [c[0] for c in extra_cols]

    # Column selector: check/uncheck to show/hide any column.
    checks = ''
    for idx, label in enumerate(all_labels):
        checks += (f'<label style="margin-right:12px;white-space:nowrap;display:inline-block;">'
                   f'<input type="checkbox" id="colchk_{idx}" checked '
                   f'onchange="toggleCol({idx}, this.checked)"> {label}</label>')
    html += (f'<div id="colselect" style="background:#fff;border-radius:8px;padding:12px 15px;'
             f'margin:10px 0;box-shadow:0 2px 4px rgba(0,0,0,0.1);font-size:12px;">'
             f'<b>Columns:</b> '
             f'<button type="button" onclick="setAllCols(true)">All</button> '
             f'<button type="button" onclick="setAllCols(false)">None</button> '
             f'<span style="margin-left:8px">{checks}</span></div>\n')

    # Table
    html += '<table id="mainTable">\n<thead>\n<tr>'
    for idx, label in enumerate(base_cols):
        html += f'<th onclick="sortTable({idx})">{label}</th>'
    for label, title in extra_cols:
        html += f'<th title="{title}">{label}</th>'
    html += '</tr>\n</thead>\n<tbody>\n'

    for story, metric, g_val, n_val, diff_pct, direction, is_regression in comparisons:
        if is_regression and abs(diff_pct) >= threshold:
            status = 'REGRESS'
            row_class = 'regress'
        elif not is_regression and abs(diff_pct) >= threshold:
            status = 'IMPROVE'
            row_class = 'improve'
        else:
            status = 'neutral'
            row_class = ''

        fps_cols = ''
        if fps_lookup:
            fl = fps_lookup.get(story, {})
            if show_fps:
                fps_cols += (f'<td style="text-align:right">{_hfps(fl.get("fps_g"))}</td>'
                             f'<td style="text-align:right">{_hfps(fl.get("fps_n"))}</td>')
            if show_df:
                # Order must match the header: for each metric, s/a/i × G/N.
                for n in ('3', '4'):
                    for tag in ('seq', 'anim', 'int'):
                        fps_cols += (f'<td style="text-align:right">{_hpct(fl.get(f"df{n}_{tag}_g"))}</td>'
                                     f'<td style="text-align:right">{_hpct(fl.get(f"df{n}_{tag}_n"))}</td>')

        html += (f'<tr class="{row_class}" data-status="{status}">'
                 f'<td>{story}</td><td>{metric}</td>'
                 f'<td style="text-align:right">{g_val:.4f}</td>'
                 f'<td style="text-align:right">{n_val:.4f}</td>'
                 f'<td style="text-align:right">{diff_pct:+.1f}%</td>'
                 f'<td>{direction}</td>'
                 f'<td><span class="status {status}">{status}</span></td>'
                 f'{fps_cols}</tr>\n')

    html += '''</tbody></table>

<h2>Per-Story Detail</h2>
<div id="storyDetails">
'''

    for story in sorted(story_summary.keys()):
        s = story_summary[story]
        html += f'''<div class="collapsible" onclick="this.classList.toggle('active')">
  <div class="story-header">
    <span><b>{story}</b></span>
    <div class="badges">
      <span class="badge r">{s["regressions"]}R</span>
      <span class="badge i">{s["improvements"]}I</span>
      <span class="badge n">{s["neutral"]}N</span>
    </div>
  </div>
</div>
<div class="collapsible-content"><table>
<tr><th>Metric</th><th>Graphite</th><th>Ganesh</th><th>Diff%</th><th>Status</th></tr>
'''
        for metric, g_val, n_val, diff_pct, direction, is_regression in s['rows']:
            if is_regression and abs(diff_pct) >= threshold:
                status = 'REGRESS'
                row_class = 'regress'
            elif not is_regression and abs(diff_pct) >= threshold:
                status = 'IMPROVE'
                row_class = 'improve'
            else:
                status = 'neutral'
                row_class = ''
            html += (f'<tr class="{row_class}"><td>{metric}</td>'
                     f'<td style="text-align:right">{g_val:.4f}</td>'
                     f'<td style="text-align:right">{n_val:.4f}</td>'
                     f'<td style="text-align:right">{diff_pct:+.1f}%</td>'
                     f'<td><span class="status {status}">{status}</span></td></tr>\n')
        html += '</table></div>\n'

    html += '''</div>

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
    // Try numeric sort
    const aNum = parseFloat(aVal.replace('%', '').replace('+', ''));
    const bNum = parseFloat(bVal.replace('%', '').replace('+', ''));
    if (!isNaN(aNum) && !isNaN(bNum)) {
      return dir === 'asc' ? aNum - bNum : bNum - aNum;
    }
    return dir === 'asc' ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
  });
  rows.forEach(row => tbody.appendChild(row));
}

function toggleCol(idx, show) {
  const table = document.getElementById('mainTable');
  for (const row of table.rows) {
    const cell = row.cells[idx];
    if (cell) cell.style.display = show ? '' : 'none';
  }
}

function setAllCols(show) {
  document.querySelectorAll('#colselect input[type=checkbox]').forEach(cb => {
    cb.checked = show;
    toggleCol(parseInt(cb.id.split('_')[1]), show);
  });
}
</script>
</div>
</body>
</html>'''

    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'\nHTML report saved to: {html_path}')


def list_metrics(results_dir):
    """List all available metrics across all results."""
    results = collect_results(results_dir)
    all_metrics = set()
    for story_data in results.values():
        for label_data in story_data.values():
            all_metrics.update(label_data.keys())

    print(f'Available metrics ({len(all_metrics)}):')
    for m in sorted(all_metrics):
        # Get unit from first occurrence
        for story_data in results.values():
            for label_data in story_data.values():
                if m in label_data:
                    unit = label_data[m].get('unit', '')
                    print(f'  {m} ({unit})')
                    break
            else:
                continue
            break


def main():
    parser = argparse.ArgumentParser(description='Analyze Graphite vs Ganesh benchmark results')
    parser.add_argument('results_dir', help='Path to results directory')
    parser.add_argument('--csv', help='Export results to CSV file')
    parser.add_argument('--html', nargs='?', const=True, default=True,
                        help='Export results to HTML report (default: enabled, filename auto-generated)')
    parser.add_argument('--no-html', action='store_true',
                        help='Disable HTML report generation')
    parser.add_argument('--metric', help='Filter by metric name (substring match)')
    parser.add_argument('--only-regressions', action='store_true',
                        help='Show only regressions')
    parser.add_argument('--threshold', type=float, default=5.0,
                        help='Threshold %% for regression/improvement (default: 5)')
    parser.add_argument('--list-metrics', action='store_true',
                        help='List all available metrics and exit')
    parser.add_argument('--no-all-metrics', action='store_true',
                        help='Show only key metrics (default: all metrics)')
    parser.add_argument('--no-fps', action='store_true',
                        help='Disable FPS extraction from trace files (default: enabled)')
    parser.add_argument('--no-df', action='store_true',
                        help='Disable PercentDroppedFrames3/4 extraction (default: enabled)')

    args = parser.parse_args()

    if not os.path.isdir(args.results_dir):
        print(f'Error: {args.results_dir} is not a directory')
        sys.exit(1)

    if args.list_metrics:
        list_metrics(args.results_dir)
        return

    # Default: all metrics unless --no-all-metrics
    if not args.no_all_metrics:
        results = collect_results(args.results_dir)
        all_metrics = set()
        for story_data in results.values():
            for label_data in story_data.values():
                all_metrics.update(label_data.keys())
        # Filter out internal metrics
        skip = {'renderingMetric_duration', 'umaMetric_duration',
                'metrics_duration', 'trace_import_duration'}
        global KEY_METRICS
        KEY_METRICS = sorted(all_metrics - skip)

    # Default HTML output: <folder_name>_results_comparison.html
    html_output = None
    if not args.no_html:
        if args.html is True or args.html is None:
            folder_name = os.path.basename(os.path.normpath(args.results_dir))
            html_output = os.path.join(args.results_dir, f'{folder_name}_results_comparison.html')
        else:
            html_output = args.html

    analyze(args.results_dir, metric_filter=args.metric,
            only_regressions=args.only_regressions,
            csv_output=args.csv, html_output=html_output, threshold=args.threshold,
            fps=not args.no_fps, df=not args.no_df)


if __name__ == '__main__':
    main()
