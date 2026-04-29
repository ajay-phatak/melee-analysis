#!/usr/bin/env python3
"""
Set Review
==========
Analyzes a set of games (multiple games vs the same opponent).

Usage:
    python set_review.py "C:/path/to/slippi" 3 --code WAWI#755
"""

import sys
import os
import argparse

from game_review import analyze, detect_port

FPS = 60


def resolve_folder(base):
    try:
        entries = os.listdir(base)
    except FileNotFoundError:
        return base
    month_dirs = sorted(
        [e for e in entries
         if os.path.isdir(os.path.join(base, e)) and len(e) == 7 and e[4] == "-"],
        reverse=True,
    )
    return os.path.join(base, month_dirs[0]) if month_dirs else base


def get_recent_slp_files(folder, count):
    resolved = resolve_folder(folder)
    files = [
        os.path.join(resolved, f)
        for f in os.listdir(resolved)
        if f.lower().endswith(".slp")
    ]
    if not files:
        return [], resolved
    files.sort(key=os.path.getmtime, reverse=True)
    return list(reversed(files[:count])), resolved


def main():
    parser = argparse.ArgumentParser(description="Analyze a set of Slippi games.")
    parser.add_argument("folder")
    parser.add_argument("count", type=int, nargs="?", default=3)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--code", type=str, default=None)
    parser.add_argument("--out", type=str, default=None)
    args = parser.parse_args()
    files, resolved = get_recent_slp_files(args.folder, args.count)
    if not files:
        print(f"No .slp files found in: {resolved}")
        sys.exit(1)
    for path in files:
        report, _ = analyze(path, focus_port=args.port, my_code=args.code)
        print(report)


if __name__ == "__main__":
    main()
