#!/usr/bin/env python3
import argparse
import subprocess
import time
import sys
import os

def normalize_chrome_dir(path):
    """Accept either a chrome.exe path or a directory; return the directory.

    graphite_vs_ganesh_dropped.py's --chrome-dir expects the directory that
    contains chrome.exe, but it's convenient to pass the full exe path too.
    """
    path = os.path.abspath(path)
    if path.lower().endswith('.exe'):
        return os.path.dirname(path)
    return path


def run_command(cmd):
    """Execute the command and return the elapsed time (in seconds).

    Output is streamed live to this process's stdout/stderr (not captured),
    so a long-running benchmark shows progress instead of appearing frozen.
    """
    start = time.perf_counter()
    # Force UTF‑8 for the child process to avoid Unicode issues
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        # No capture_output -> child inherits our stdout/stderr and streams live.
        subprocess.run(cmd, check=True, env=env)
    except subprocess.CalledProcessError as e:
        print(f"Command failed with return code {e.returncode}", file=sys.stderr)
    end = time.perf_counter()
    return end - start

def run_stats(cmd, runs, label):
    """Run the given command `runs` times, print per-run and summary stats.

    Returns the list of durations (seconds).
    """
    times = []
    print(f"Starting {runs} test run(s) for: {label}\n")
    for i in range(1, runs + 1):
        print(f"[{label}] Run {i}:")
        duration = run_command(cmd)
        times.append(duration)
        print(f"  Duration: {duration:.2f} seconds\n")

    if times:
        avg = sum(times) / len(times)
        print(f"========== Statistics [{label}] ==========")
        print(f"Total runs: {len(times)}")
        print(f"Average time: {avg:.2f} seconds")
        print(f"Minimum time: {min(times):.2f} seconds")
        print(f"Maximum time: {max(times):.2f} seconds")
        print("=" * (len(label) + 30))
        print()
    else:
        print(f"No runs recorded for {label}.\n")
    return times


def main():
    parser = argparse.ArgumentParser(
        description='Repeat the graphite_vs_ganesh_dropped benchmark, '
                    'optionally against several Chrome builds')
    parser.add_argument('--chrome-dir', nargs='+', default=None,
                        metavar='PATH',
                        help='One or more Chrome locations to test, each run '
                             'separately. Accepts either a directory containing '
                             'chrome.exe or the full path to chrome.exe. '
                             '(default: use graphite_vs_ganesh_dropped.py\'s '
                             'built-in Chrome path)')
    parser.add_argument('--runs', type=int, default=3,
                        help='Number of runs per Chrome (default: 3)')
    args = parser.parse_args()

    # Get the directory where this script lives
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Build absolute path to the target script
    target_script = os.path.join(script_dir, "graphite_vs_ganesh_dropped.py")

    # Check if the target script exists
    if not os.path.isfile(target_script):
        print(f"ERROR: Target script not found: {target_script}", file=sys.stderr)
        sys.exit(1)

    # Build the list of (label, chrome_dir) targets. None => use the target
    # script's built-in default Chrome.
    #if args.chrome_dir:
    #    chrome_dirs = [normalize_chrome_dir(p) for p in args.chrome_dir]
    #else:
    #    chrome_dirs = [None]

    if args.chrome_dir:
        chrome_dirs = [normalize_chrome_dir(p) for p in args.chrome_dir]
    else:
        # 用户未指定时默认测试这两个 Chrome
        DEFAULT_CHROME_DIR = os.path.join(
            os.environ['LOCALAPPDATA'], 'Google', 'Chrome SxS', 'Application'
        )
        DEFAULT_CHROME_EXE_DIR = os.path.join(DEFAULT_CHROME_DIR, 'chrome.exe')
        default_paths = [
            #r"C:\temp\chrome-disabledepth\Chrome-bin\chrome.exe",
            #r"C:\temp\chrome\Chrome-bin\chrome.exe",
            DEFAULT_CHROME_EXE_DIR
        ]
        chrome_dirs = [normalize_chrome_dir(p) for p in default_paths]

    all_stats = {}
    print("Before Loop")
    for chrome_dir in chrome_dirs:
        label = chrome_dir if chrome_dir else "default-chrome"
        # --chrome-dir is a top-level option and must precede the "run" subcommand.
        cmd = [sys.executable, target_script]
        if chrome_dir:
            cmd += ["--chrome-dir", chrome_dir]
        cmd += ["run"]

        print("#" * 70)
        print(f"# Chrome: {label}")
        print("#" * 70)
        all_stats[label] = run_stats(cmd, args.runs, label)

    # Cross-Chrome comparison when more than one Chrome was tested.
    if len(all_stats) > 1:
        print("========== Comparison across Chrome builds ==========")
        for label, times in all_stats.items():
            if times:
                avg = sum(times) / len(times)
                print(f"  {label}: avg {avg:.2f}s over {len(times)} run(s)")
            else:
                print(f"  {label}: no runs recorded")
        print("====================================================")

if __name__ == "__main__":
    main()