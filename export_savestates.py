#!/usr/bin/env python3
"""
Export Savestates
==================
session_review.py's --json output carries a "moments" list per set: deaths,
missed edgeguards, and best punishes (see _set_moments in session_review.py).
Those are great for re-*watching*, but a missed edgeguard you only watch is a
missed edgeguard you're likely to miss again. This script turns a moment into
a Training Mode - Community Edition savestate — a .gci file in Dolphin's GCI
memory-card folder — so you can load it and retry the exact situation with
full control, instead of just watching the replay again.

It's a CLI port of an Electron app's savestate.ts. The .gci construction itself
is done by a prebuilt Rust sidecar (nojohns-savestate, built from ./savestate/)
that already knows how to build a Melee savestate and patch it into Training
Mode's memory-card format. This script does everything around that: find the
moments, find the folders, write the sidecar's job spec, run it, and report
what happened in plain language.

Usage:
    python export_savestates.py list                       # missed edgeguards only
    python export_savestates.py list --kind all --set 2
    python export_savestates.py export --pick 1,3,5-7
    python export_savestates.py export --out-dir "D:/Dolphin/GC/USA/Card A" --limit 10
    python export_savestates.py export --dry-run            # inspect the job spec, write nothing

`session.json` is produced by:  session_review.py ... --json session.json
"""

import os
import sys
import io
import re
import json
import tempfile
import subprocess
import argparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Every frame from session.json is already native Melee numbering, but the
# sidecar wants an index into the replay's raw frame array (which starts at 0
# for Melee frame -123). Shift explicitly rather than hardcoding 123.
MELEE_FRAME_START = -123

VALID_KINDS = ("death", "missed_edgeguard", "best_punish")
DEFAULT_KINDS = ("missed_edgeguard",)  # the headline use case
KIND_TAG = {"death": "Death", "missed_edgeguard": "Edgeguard", "best_punish": "Punish"}
DEFAULT_LIMIT = 24  # a GCI-folder memory card fills up; the Electron app used the same cap
TAIL = os.path.join("GC", "USA", "Card A")  # Dolphin's Slot-A GCI-folder layout


# --- session.json + moment selection ----------------------------------------
def default_json_path():
    """session.json next to this script, not the caller's cwd (this is meant
    to be invoked from anywhere, e.g. a coaching skill, not just the repo)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "session.json")


def load_session(path):
    if not os.path.isfile(path):
        print(f"session.json not found at: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_kinds(kind_args):
    """--kind is repeatable; omitted = missed_edgeguard only; 'all' anywhere in
    the list means every kind, regardless of what else was passed."""
    if not kind_args:
        return set(DEFAULT_KINDS)
    return set(VALID_KINDS) if "all" in kind_args else set(kind_args)


def select_sets(session, set_index):
    """Sets to search, honoring --set (1-based). Returns (sets, error)."""
    sets = session.get("sets", []) or []
    if set_index is None:
        return sets, None
    if not (1 <= set_index <= len(sets)):
        return [], f"--set {set_index} out of range (session has {len(sets)} set(s))"
    return [sets[set_index - 1]], None


def matching_moments(sets, kinds):
    """(index, set_record, moment) for every moment matching `kinds`, numbered
    1..N in the order `list` prints them. list and export share this function
    so --pick's numbering always matches what list showed."""
    out = []
    for s in sets:
        for m in s.get("moments", []) or []:
            if m.get("kind") in kinds:
                out.append((len(out) + 1, s, m))
    return out


def _set_label(s):
    return f"{s.get('my_char', '?')}-{s.get('opp_char', '?')} vs {s.get('opp_code', 'Unknown')}"


def _timestamp(frame):
    """mm:ss at 60fps, frame 0 = 0:00 (pre-match frames clamp to 0:00)."""
    m, s = divmod(max(0, frame) // 60, 60)
    return f"{m}:{s:02d}"


def parse_pick(spec):
    """'1,3,5-7' -> {1,3,5,6,7} (1-based, inclusive ranges)."""
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)-(\d+)$", part)
        if m:
            lo, hi = sorted((int(m.group(1)), int(m.group(2))))
            out.update(range(lo, hi + 1))
        elif part.isdigit():
            out.add(int(part))
        else:
            raise ValueError(f"bad --pick entry: '{part}' (expected N or N-M)")
    return out


# --- path resolution: Dolphin's GCI memory-card folder ----------------------
def _accept_out_dir(path):
    """Accept if the folder exists, OR its parent exists — Dolphin only
    creates 'Card A' once Slot A is set to GCI-folder mode, and the sidecar
    creates it on write, so not-yet-existing is normal. Reject only if neither
    exists, so a typo doesn't silently write savestates into nowhere."""
    if not path:
        return False
    return os.path.isdir(path) or os.path.isdir(os.path.dirname(os.path.normpath(path)) or "")


def detect_card_a():
    """Fixed candidate list, first existing wins. <Documents> is tried as both
    the plain profile path and the OneDrive-redirected one, since either can
    be the real Documents folder depending on the machine."""
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    docs_dirs = [os.path.join(home, "Documents"), os.path.join(home, "OneDrive", "Documents")]
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(os.path.join(appdata, "Slippi Launcher", "netplay", "User", TAIL))
    for shape in ("Dolphin Emulator", os.path.join("Dolphin", "FM-Slippi", "User"),
                  os.path.join("Slippi", "netplay", "User")):
        candidates += [os.path.join(d, shape, TAIL) for d in docs_dirs]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


NO_CARD_MSG = ("point --out-dir at your Dolphin GCI memory-card folder (Slot A must be set to "
               "'GCI Folder' in Dolphin's config)")
NO_EXPORTER_MSG = "build it with `cargo build --release` in savestate/, or set MELEE_SAVESTATE_EXE"


def resolve_out_dir(cli_arg):
    """First existing wins: --out-dir, env MELEE_SAVESTATE_CARD_A, then
    detection. An explicit --out-dir/env value that fails acceptance is a hard
    error rather than falling through — the user named a specific place, so
    silently trying elsewhere risks writing savestates where they won't look."""
    for source, value in (("--out-dir", cli_arg),
                          ("MELEE_SAVESTATE_CARD_A", os.environ.get("MELEE_SAVESTATE_CARD_A"))):
        if not value:
            continue
        if _accept_out_dir(value):
            return value, None
        return None, (f"{source} '{value}' not found (its folder or parent must already exist) "
                       f"-- {NO_CARD_MSG}")
    detected = detect_card_a()
    return (detected, None) if detected else (None, f"could not find a Dolphin GCI memory-card "
                                                       f"folder -- {NO_CARD_MSG}")


def resolve_exporter(cli_arg):
    """Same first-existing-wins / hard-error-on-explicit-miss shape as
    resolve_out_dir, but over the sidecar binary instead of the card folder."""
    for source, value in (("--exporter", cli_arg),
                          ("MELEE_SAVESTATE_EXE", os.environ.get("MELEE_SAVESTATE_EXE"))):
        if not value:
            continue
        if os.path.isfile(value):
            return value, None
        return None, f"{source} '{value}' not found -- {NO_EXPORTER_MSG}"

    script_dir = os.path.dirname(os.path.abspath(__file__))
    rel_bases = [os.path.join("savestate", "nojohns-savestate"),
                 os.path.join("savestate", "dist", "nojohns-savestate"),
                 os.path.join("savestate", "target", "release", "nojohns-savestate")]
    # Windows prefers .exe; a msys/WSL-style build without the suffix should
    # still be found, and non-Windows is the mirror image.
    exts = (".exe", "") if os.name == "nt" else ("", ".exe")
    for base in rel_bases:
        for ext in exts:
            candidate = os.path.join(script_dir, base + ext)
            if os.path.isfile(candidate):
                return candidate, None
    return None, f"could not find nojohns-savestate -- {NO_EXPORTER_MSG}"


# --- moment filtering + job construction ------------------------------------
def skip_reason(moment):
    """Why a moment can't be exported, or None. The two reasons are deduped by
    message when reported, so 40 stale moments don't print 40 identical lines."""
    if (moment.get("human_port") is None or moment.get("save_frame") is None
            or moment.get("save_frames") is None):
        return "re-run the analysis to export these"
    path = moment.get("path")
    if not path or not os.path.isfile(path):
        return "replay file moved or deleted"
    return None


def build_job(job_id, moment, set_record, sheik_fix):
    my_char = set_record.get("my_char", "?")
    tag = KIND_TAG.get(moment["kind"], moment["kind"])
    # Tag first: Training Mode truncates the tail, so the identifying part
    # must survive even on a long matchup name.
    name = f"{tag} g{moment['game_index']} f{moment['frame']} {my_char}-{set_record.get('opp_char', '?')}"
    return {
        "id": str(job_id),
        "slp": moment["path"],
        "human": moment["human_port"],
        "start_frame": moment["save_frame"] - MELEE_FRAME_START,
        "num_frames": moment["save_frames"],
        "name": name,
        "swap_sheik_zelda": bool(sheik_fix) and my_char.strip().lower() in ("sheik", "zelda"),
    }


def run_sidecar(exe, jobs_path):
    """Run the sidecar and parse its --ndjson stream.
    Returns (exit_code, per_job_results, summary_or_None, stderr_text)."""
    proc = subprocess.run([exe, "--jobs", jobs_path, "--ndjson"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    results, summary = [], None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" in obj:
            results.append(obj)
        else:
            summary = obj
    return proc.returncode, results, summary, proc.stderr


# --- subcommands -------------------------------------------------------
def cmd_list(args):
    session = load_session(args.json_path)
    kinds = resolve_kinds(args.kind)
    sets, err = select_sets(session, args.set)
    if err:
        print(err, file=sys.stderr)
        return 1

    matches = matching_moments(sets, kinds)
    if not matches:
        all_sets = session.get("sets", []) or []
        if not all_sets:
            print("session.json has no sets.")
        elif not any(s.get("moments") for s in all_sets):
            print("This session.json predates the moments feature (no 'moments' data). "
                  "Re-run session_review.py with --json on the current pipeline to get moments.")
        else:
            print(f"No moments matched kind={sorted(kinds)}"
                  + (f", set {args.set}" if args.set else "") + ".")
        return 1

    print(f"  {'#':<4} {'Set':<26} {'G':>2} {'Frame':>7} {'Time':>5}  Label  [file]")
    print("  " + "-" * 100)
    for idx, s, m in matches:
        fname = m.get("file") or os.path.basename(m.get("path") or "") or "?"
        print(f"  {idx:<4} {_set_label(s):<26.26} {m.get('game_index', '?'):>2} "
              f"{m.get('frame', 0):>7} {_timestamp(m.get('frame', 0)):>5}  "
              f"{m.get('label', '')}  [{fname}]")
    print(f"\n  {len(matches)} moment(s). Export with: "
          f"python export_savestates.py export --pick 1,3,5-7")
    return 0


def cmd_export(args):
    session = load_session(args.json_path)
    kinds = resolve_kinds(args.kind)
    sets, err = select_sets(session, args.set)
    if err:
        print(err, file=sys.stderr)
        return 1

    matches = matching_moments(sets, kinds)
    if not matches:
        print(f"No moments matched kind={sorted(kinds)}"
              + (f", set {args.set}" if args.set else "") + ". Nothing to export.")
        return 1

    if args.pick is not None:
        try:
            picked = parse_pick(args.pick)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        matches = [t for t in matches if t[0] in picked]
        if not matches:
            print("--pick matched none of the listed moments.")
            return 1

    # Filter un-exportable moments, then cap. Order matters: --pick indexes
    # into the full list (matching `list`), skip-filtering and the cap apply
    # to what's left, in that order, so --limit caps real exports, not skips.
    skip_counts = {}
    exportable = []
    for idx, s, m in matches:
        reason = skip_reason(m)
        if reason:
            skip_counts[reason] = skip_counts.get(reason, 0) + 1
        else:
            exportable.append((idx, s, m))

    dropped = max(0, len(exportable) - args.limit)
    exportable = exportable[:args.limit]

    for reason, n in sorted(skip_counts.items(), key=lambda kv: -kv[1]):
        print(f"Skipped {n} moment(s): {reason}")
    if dropped:
        print(f"Capped at --limit {args.limit}: dropped {dropped} moment(s) "
              f"(raise --limit to include them).")
    if not exportable:
        print("Nothing left to export after filtering.")
        return 1

    out_dir, out_err = resolve_out_dir(args.out_dir)
    if out_err:
        print(out_err, file=sys.stderr)
        return 1
    exe, exe_err = resolve_exporter(args.exporter)
    if not args.dry_run and exe_err:
        print(exe_err, file=sys.stderr)
        return 1

    jobs = [build_job(i, m, s, args.sheik_fix) for i, (_, s, m) in enumerate(exportable)]
    spec = {"out_dir": out_dir, "jobs": jobs}
    fd, jobs_path = tempfile.mkstemp(prefix="melee_savestate_jobs_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2)

    if args.dry_run:
        print(f"[dry-run] out_dir  : {out_dir}")
        print(f"[dry-run] exporter : {exe or f'(unresolved: {exe_err})'}")
        print(f"[dry-run] job spec : {jobs_path}")
        print(json.dumps(spec, indent=2))
        return 0

    try:
        code, results, summary, stderr_text = run_sidecar(exe, jobs_path)
    finally:
        try:
            os.remove(jobs_path)
        except OSError:
            pass

    if code != 0 and summary is None:
        print(f"Exporter failed (exit {code}): {stderr_text.strip() or '(no message)'}",
              file=sys.stderr)
        return 1

    written = [r["file"] for r in results if r.get("ok")]
    error_counts = {}
    for r in results:
        if not r.get("ok"):
            error_counts[r.get("msg", "unknown error")] = error_counts.get(r.get("msg", "unknown error"), 0) + 1

    if written:
        print(f"Wrote {len(written)} savestate(s) to {out_dir}:")
        for f in written:
            print(f"  {f}")
    for msg, n in sorted(error_counts.items(), key=lambda kv: -kv[1]):
        print(f"Failed {n} moment(s): {msg}")

    return 0 if written else 1


# --- CLI ---------------------------------------------------------------
def _add_common_args(p):
    p.add_argument("--json", dest="json_path", default=None,
                   help="Path to session_review.py --json output "
                        "(default: session.json next to this script)")
    p.add_argument("--kind", action="append", choices=list(VALID_KINDS) + ["all"],
                   help="Moment kind to include (repeatable). Default: missed_edgeguard only.")
    p.add_argument("--set", type=int, default=None,
                   help="Limit to the Nth set in the file (1-based). Default: all sets.")


def main():
    parser = argparse.ArgumentParser(
        description="List and export notable moments (deaths, missed edgeguards, best "
                     "punishes) as Training Mode - Community Edition savestates.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List moments matching the filters.")
    _add_common_args(p_list)

    p_exp = sub.add_parser("export", help="Export matching moments as .gci savestates.")
    _add_common_args(p_exp)
    p_exp.add_argument("--pick", default=None,
                        help="Comma-separated indices from `list` output, ranges allowed "
                             "(e.g. 1,3,5-7). Default: every matching moment.")
    p_exp.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Max moments to export (default {DEFAULT_LIMIT}).")
    p_exp.add_argument("--out-dir", dest="out_dir", default=None,
                        help="Dolphin's GCI memory-card folder (Slot A). "
                             "Default: MELEE_SAVESTATE_CARD_A env var, then auto-detect.")
    p_exp.add_argument("--exporter", default=None,
                        help="Path to nojohns-savestate(.exe). "
                             "Default: MELEE_SAVESTATE_EXE env var, then savestate/ in this repo.")
    p_exp.add_argument("--sheik-fix", dest="sheik_fix", action="store_true", default=True,
                        help="(default) Set swap_sheik_zelda for Sheik/Zelda sets, working "
                             "around a Training Mode header bug on builds before TM-CE v1.3.")
    p_exp.add_argument("--no-sheik-fix", dest="sheik_fix", action="store_false",
                        help="Turn off the Sheik/Zelda swap (TM-CE v1.3+ doesn't need it).")
    p_exp.add_argument("--dry-run", action="store_true",
                        help="Write/print the job spec and resolved paths; don't run the exporter.")

    args = parser.parse_args()
    if args.json_path is None:
        args.json_path = default_json_path()

    if args.cmd == "list":
        sys.exit(cmd_list(args))
    elif args.cmd == "export":
        sys.exit(cmd_export(args))


if __name__ == "__main__":
    main()
