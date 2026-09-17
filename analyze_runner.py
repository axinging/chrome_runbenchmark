#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import subprocess
import sys
import argparse

def run_analysis(root_dir, script_name="analyze_droppedframe.py"):
    """
    Iterate through all subdirectories in root_dir and run:
    python3 script_name --html <subdir>.html <subdir>
    """
    # Save original working directory to restore later
    original_cwd = os.getcwd()
    # Get analyze_droppedframe
    script_name_prefix = os.path.splitext(os.path.basename(script_name))[0]
    try:
        # Change to the root directory so that HTML files are generated there
        os.chdir(root_dir)
        # Iterate over all items in the root directory
        for item in os.listdir('.'):
            if os.path.isdir(item):
                # Build the command
                html_file = script_name_prefix + "-" + item + ".html"
                csv_file = script_name_prefix + "-" + item + ".csv"
                cmd = ["python", script_name, "--html", html_file,"--csv", csv_file, item]
                print(f"Running: {' '.join(cmd)}")
                # Execute the command
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    print(f"Error running command for {item}: {result.stderr}")
                else:
                    print(f"Success for {item}")
    finally:
        # Restore the original working directory
        os.chdir(original_cwd)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run analyze_droppedframe.py for each subfolder in a directory."
    )
    parser.add_argument(
        "root_dir", nargs='?', default='.',
        help="Root directory containing subfolders (default: current directory)"
    )
    parser.add_argument(
        "--script", default="analyze_droppedframe.py",
        help="Name of the analysis script (default: analyze_droppedframe.py)"
    )
    args = parser.parse_args()
    run_analysis(args.root_dir, args.script)