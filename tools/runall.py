#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
依次运行若干脚本，每个脚本运行结束后等待若干秒再运行下一个。

用法：
    python runall.py collect_tracing.py run_fps.py
    python runall.py --interval 60 collect_tracing.py run_fps.py
"""

import argparse
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INTERVAL = 120


def main():
    parser = argparse.ArgumentParser(description="按顺序运行多个脚本，脚本之间间隔一段时间")
    parser.add_argument("scripts", nargs="+", help="要依次运行的脚本文件名（相对 runall.py 所在目录，或绝对路径）")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help=f"每个脚本之间的间隔秒数（默认 {DEFAULT_INTERVAL}）")
    args = parser.parse_args()

    total = len(args.scripts)
    for idx, script in enumerate(args.scripts, 1):
        script_path = script if os.path.isabs(script) else os.path.join(SCRIPT_DIR, script)
        if not os.path.exists(script_path):
            print(f"[SKIP] 找不到脚本: {script_path}")
            continue

        print("=" * 64)
        print(f"[{idx}/{total}] 运行: {script_path}")
        print("=" * 64)
        result = subprocess.run([sys.executable, script_path])
        if result.returncode != 0:
            print(f"[WARN] {script} 退出码非 0: {result.returncode}")

        if idx < total:
            print(f"\n等待 {args.interval} 秒后运行下一个脚本...\n")
            time.sleep(args.interval)

    print("\n全部脚本运行完成。")


if __name__ == "__main__":
    main()
