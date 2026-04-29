#!/usr/bin/env python3
"""
fetch_pro_replays.py
====================
Downloads matchup replays from the HuggingFace tournament dataset,
optionally filtering for specific connect codes.

The dataset is organized by character directory. --matchup specifies which
directory to search and (optionally) which opponent to filter for within it.

Usage:
    python fetch_pro_replays.py --matchup SHEIK/FALCO --out pro_replays/
    python fetch_pro_replays.py --matchup FALCO --out pro_replays/
    python fetch_pro_replays.py --matchup SHEIK/FALCO --codes "JM#0" --dry-run
"""

import sys
import os
import json
import argparse
import subprocess
import tempfile
import time

from game_review import get_netplay_info

DATASET  = "erickfm/slippi-public-dataset-v3.7"
BASE_URL = f"https://huggingface.co/datasets/{DATASET}/resolve/main"
API_URL  = f"https://huggingface.co/api/datasets/{DATASET}/tree/main"

# Windows Schannel rejects the HF CDN cert chain with TLS 1.3;
# curl --insecure --tls-max 1.2 works reliably.
CURL_BASE = ["curl", "-L", "--insecure", "--tls-max", "1.2", "--silent", "--fail"]

MAX_RETRIES = 3
RETRY_DELAYS = [3, 10, 30]


def hf_get(url, retries=MAX_RETRIES):
    """Fetch URL bytes using curl (works around Windows TLS issues with HF CDN)."""
    last_err = None
    for attempt in range(retries):
        try:
            result = subprocess.run(
                CURL_BASE + [url],
                capture_output=True,
                timeout=120,
                check=True,
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            last_err = RuntimeError(f"curl exit {e.returncode}: {e.stderr.decode(errors='replace').strip()}")
        except Exception as e:
            last_err = e
        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
        print(f"  [retry {attempt+1}/{retries}] {last_err} — waiting {delay}s")
        time.sleep(delay)
    raise last_err


CACHE_FILE = "hf_file_list_cache.json"

# Maps dataset directory names to the string that appears in filenames
CHAR_FILENAME_MAP = {
    "CPTFALCON":   "Captain Falcon",
    "ZELDA_SHEIK": "Sheik",
    "ICE_CLIMBERS": "Ice Climbers",
    "GAMEANDWATCH": "Game And Watch",
}


def list_matchup_files(char_dir, filter_char=None, cache=True):
    """Return file paths in char_dir, optionally filtered by opponent name."""
    cache_key = f"{char_dir}_{filter_char or 'ALL'}"

    if cache and os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            cached = json.load(f)
        if cache_key in cached:
            print(f"  (using cached file list: {len(cached[cache_key])} files)")
            return cached[cache_key]

    url = f"{API_URL}/{char_dir}?recursive=false"
    data = json.loads(hf_get(url))
    all_paths = [f["path"] for f in data if f.get("path", "").endswith(".slp")]

    if filter_char:
        import re
        # Resolve alias (e.g. CPTFALCON → "Captain Falcon")
        search_term = CHAR_FILENAME_MAP.get(filter_char.upper(), filter_char)
        # Also resolve the directory char for ditto detection
        dir_char_term = CHAR_FILENAME_MAP.get(char_dir.upper(), char_dir.capitalize())
        is_ditto = search_term.lower() == dir_char_term.lower()

        pattern = re.compile(r"(?<![A-Za-z])" + re.escape(search_term) + r"(?![A-Za-z])", re.IGNORECASE)
        if is_ditto:
            # Ditto: require the character name to appear at least twice in the filename
            paths = [p for p in all_paths if len(pattern.findall(os.path.basename(p))) >= 2]
        else:
            # Word-boundary match against filename only (avoids Falco matching Falcon)
            paths = [p for p in all_paths if pattern.search(os.path.basename(p))]
    else:
        paths = all_paths

    if cache:
        existing = {}
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                existing = json.load(f)
        existing[cache_key] = paths
        with open(CACHE_FILE, "w") as f:
            json.dump(existing, f, indent=2)
        print(f"  (cached {len(paths)} file paths to {CACHE_FILE})")

    return paths


def url_encode_path(hf_path):
    """Encode path for HuggingFace URL, preserving slashes."""
    safe = "/:@!$&'()*,;="  # chars safe in URL path
    return "".join(
        c if (c.isalnum() or c in safe or c in "-._~") else f"%{ord(c):02X}"
        for c in hf_path
    )


def download_and_check(hf_path, target_codes, out_dir, dry_run=False):
    """Download a file, check if it contains a target code, keep or discard.

    If target_codes is empty, save all parseable files (no-filter mode).
    """
    url = f"{BASE_URL}/{url_encode_path(hf_path)}"
    filename = os.path.basename(hf_path)

    try:
        data = hf_get(url)
    except Exception as e:
        print(f"  SKIP (download error): {filename} — {e}")
        return False

    with tempfile.NamedTemporaryFile(suffix=".slp", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        info = get_netplay_info(tmp_path)
        codes_in_file = {v["code"].upper() for v in info.values() if v.get("code")}

        if target_codes:
            matched = codes_in_file & {c.upper() for c in target_codes}
            keep = bool(matched)
            label = ', '.join(matched) if matched else None
        else:
            # No-filter mode: keep all parseable files
            keep = True
            label = ', '.join(codes_in_file) if codes_in_file else "no codes"

        if keep:
            if not dry_run:
                os.makedirs(out_dir, exist_ok=True)
                dest = os.path.join(out_dir, filename)
                os.replace(tmp_path, dest)
                print(f"  SAVED ({label}): {filename}")
            else:
                print(f"  MATCH ({label}): {filename}")
            return True
        else:
            print(f"  skip: {filename} — codes: {codes_in_file or 'none'}")
            return False
    except Exception as e:
        print(f"  SKIP (parse error): {filename} — {e}")
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Fetch pro player replays from HuggingFace dataset.")
    parser.add_argument("--matchup", type=str, required=True,
                        help="Character directory, optionally with opponent: FALCO or SHEIK/FALCO")
    parser.add_argument("--codes",   type=str, default="",
                        help="Comma-separated connect codes to filter for (e.g. 'JM#0,CODY#007'). Omit to save all.")
    parser.add_argument("--out",     type=str, default="pro_replays",
                        help="Output directory for matched replays")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matches without saving")
    parser.add_argument("--no-cache", action="store_true",
                        help="Force fresh API listing (don't use cached file list)")
    parser.add_argument("--list-only", action="store_true",
                        help="Just list and cache the file paths, don't download")
    args = parser.parse_args()

    # Parse matchup: "SHEIK/FALCO" → dir=SHEIK, filter=Falco
    #                "FALCO"       → dir=FALCO, filter=None
    if "/" in args.matchup:
        char_dir, opp = args.matchup.upper().split("/", 1)
        filter_char = opp.capitalize()
    else:
        char_dir = args.matchup.upper()
        filter_char = None

    target_codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    matchup_str = f"{char_dir}/{filter_char}" if filter_char else char_dir
    if target_codes:
        print(f"Searching {matchup_str} for codes: {target_codes}")
    else:
        print(f"Searching {matchup_str} — no code filter (saving all parseable files)")

    files = list_matchup_files(char_dir, filter_char=filter_char, cache=not args.no_cache)
    print(f"Found {len(files)} files in dataset\n")

    if args.list_only:
        for p in files:
            print(f"  {p}")
        return

    found = 0
    for hf_path in files:
        if download_and_check(hf_path, target_codes, args.out, dry_run=args.dry_run):
            found += 1

    print(f"\nDone. {found}/{len(files)} files matched.")
    if found > 0 and not args.dry_run:
        print(f"Saved to: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
