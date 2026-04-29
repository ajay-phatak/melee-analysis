#!/usr/bin/env python3
"""
Session Review (truncated stub - full version in commit history elsewhere)
==============
Aggregates all games from a session, groups them into sets by opponent,
and shows per-set summaries plus overall session totals.

Usage:
    python session_review.py "C:/path/to/slippi" --code WAWI#755

Note: this file was uploaded as a placeholder. The complete version of
session_review.py exceeds the upload tool's size limits during automated
upload. Re-upload the local file from your project to overwrite this stub.
"""

import sys
import argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder")
    parser.add_argument("--code", required=True)
    parser.add_argument("--sets", type=int, default=None)
    parser.add_argument("--count", type=int, default=None)
    parser.add_argument("--out", type=str, default=None)
    args = parser.parse_args()
    print("Stub: please replace this file with the full session_review.py from your local project.")


if __name__ == "__main__":
    main()
