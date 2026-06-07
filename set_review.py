#!/usr/bin/env python3
"""
Set Review
==========
Analyzes a set of games (multiple games vs the same opponent).

Usage:
    python set_review.py "C:/path/to/slippi" 3 --code ABCD#123
    python set_review.py "C:/path/to/slippi" 3 --port 1
    python set_review.py "C:/path/to/slippi" --files game1.slp game2.slp --code ABCD#123
    python set_review.py "C:/path/to/slippi" 3 --code ABCD#123 --out set.txt
"""

import sys
import os
import argparse

from game_review import analyze, detect_port, LEDGE_OPTIONS

FPS = 60


def resolve_folder(base):
    """If base contains YYYY-MM subfolders, return the most recent one."""
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


def set_report(folder, count, focus_port=None, my_code=None, files=None):
    if files:
        resolved = folder
    else:
        files, resolved = get_recent_slp_files(folder, count)
    if not files:
        print(f"No .slp files found in: {resolved}")
        sys.exit(1)

    # Auto-detect port from connect code using first parseable file
    if focus_port is None and my_code:
        for f in files:
            p = detect_port(f, my_code)
            if p is not None:
                focus_port = p
                break

    lines = []
    def out(s=""): lines.append(s)

    out("=" * 70)
    out("  SET REVIEW")
    out("=" * 70)
    out(f"  Folder : {resolved}")
    out(f"  Games  : {len(files)} (requested {count})")
    if my_code:
        out(f"  Code   : {my_code}  (port {focus_port + 1 if focus_port is not None else '?'})")
    out()

    game_summaries = []
    errors = []

    for path in files:
        _, game_data = analyze(path, focus_port=focus_port, my_code=my_code)
        if game_data is None:
            errors.append(f"Failed to parse {os.path.basename(path)}")
        else:
            game_data["file"] = os.path.basename(path)
            game_summaries.append(game_data)

    if errors:
        out("  PARSE ERRORS")
        for e in errors:
            out(f"    [!] {e}")
        out()

    if not game_summaries:
        out("  No games could be parsed.")
        return "\n".join(lines)

    all_ports = sorted({p for g in game_summaries for p in g["port_order"]})

    def write_player_section(port_idx, label_prefix=""):
        port_games = [g for g in game_summaries if port_idx in g["ports"]]
        if not port_games:
            return
        pdata = [g["ports"][port_idx] for g in port_games]
        char_label = pdata[0]["label"]
        code = pdata[0].get("netplay_code", "")
        code_str = f"  ({code})" if code else ""

        out("=" * 70)
        out(f"  {label_prefix}{char_label}{code_str}")
        out("=" * 70)

        # Per-game table
        out()
        out("  PER-GAME TABLE")
        out("-" * 70)
        hdr = f"  {'#':<4} {'Stage':<22} {'Stk':<5} {'AvgPun':>7} {'L-cnc':>7} {'WD%':>6} {'F1%':>6} {'Ctr%':>6} {'Shld':>6}"
        out(hdr)
        out("  " + "-" * 68)

        for gnum, g in enumerate(game_summaries, 1):
            if port_idx not in g["ports"]:
                continue
            p  = g["ports"][port_idx]
            ts = p["tech_skill"]
            sc = p["stage_control"]
            nt = p["neutral"]
            pu = p["punishes"]

            l_rate  = f"{ts['l_cancel_rate']:.0f}%" if ts["l_cancel_attempts"] > 0 else "N/A"
            wd_rate = f"{ts['wd_rate']:.0f}%"       if ts["wd_attempts"] > 0       else "N/A"
            f1_rate = f"{ts['f1_rate']:.0f}%"       if ts["f1_attempts"] > 0       else "N/A"
            result  = "W" if p["won"] else "L"

            out(f"  {gnum}{result:<3} {g['stage'][:20]:<22} {p['stocks_lost']:<5} {pu['avg_damage_dealt']:>7.1f} "
                f"{l_rate:>7} {wd_rate:>6} {f1_rate:>6} "
                f"{sc['center_pct']:>5.1f}% {nt['shield_seconds']:>5.1f}s")
        out()

        # Aggregate trends
        out("  SET TRENDS")
        out("-" * 70)

        n = len(pdata)
        def avg(fn): return sum(fn(p) for p in pdata) / n

        total_high   = sum(p["tech_skill"]["high_aerials"]       for p in pdata)
        total_low    = sum(p["tech_skill"]["low_aerials"]        for p in pdata)
        total_lc_att = sum(p["tech_skill"]["l_cancel_attempts"] for p in pdata)
        total_lc_suc = sum(p["tech_skill"]["l_cancel_success"]  for p in pdata)
        lc_rate = (100.0 * total_lc_suc / total_lc_att) if total_lc_att > 0 else None

        total_wd_att = sum(p["tech_skill"]["wd_attempts"] for p in pdata)
        total_wd_prf = sum(p["tech_skill"]["wd_perfect"]  for p in pdata)
        wd_rate = (100.0 * total_wd_prf / total_wd_att) if total_wd_att > 0 else None

        total_f1_att = sum(p["tech_skill"]["f1_attempts"] for p in pdata)
        total_f1_prf = sum(p["tech_skill"]["f1_perfect"]  for p in pdata)
        f1_rate = (100.0 * total_f1_prf / total_f1_att) if total_f1_att > 0 else None

        # Ledge tech sums
        def _lt(key): return sum(p["ledge_tech"].get(key, 0) for p in pdata if "ledge_tech" in p)
        lt_opts = {o: sum(p["ledge_tech"]["option_counts"].get(o, 0)
                          for p in pdata if "ledge_tech" in p) for o in LEDGE_OPTIONS}
        lt_eng, lt_hang, lt_hinv = _lt("engagements"), _lt("hang_frames"), _lt("hang_invuln_frames")
        lt_dwell, lt_dwn = _lt("dwell_frames"), _lt("dwell_n")
        lt_ld, lt_gsum, lt_gn = _lt("ledgedash_count"), _lt("galint_sum"), _lt("galint_n")
        lt_gmax = max((p["ledge_tech"].get("galint_max", 0) for p in pdata if "ledge_tech" in p), default=0)
        lt_gpos = _lt("galint_pos")
        lt_react, lt_fsum, lt_fn = _lt("ld_reaction_sum"), _lt("ld_fall_sum"), _lt("ld_fall_n")
        lt_wl = _lt("ld_waveland_sum")
        lt_dist = sum(p["ledge_tech"].get("ld_distance_sum", 0.0) for p in pdata if "ledge_tech" in p)

        dealt_seqs = []
        for g in port_games:
            opp_ports = [pi for pi in g["port_order"] if pi != port_idx]
            if opp_ports and opp_ports[0] in g["ports"]:
                dealt_seqs.extend(g["ports"][opp_ports[0]]["punishes"]["sequences"])
        avg_punish_dealt = sum(s["damage"] for s in dealt_seqs) / len(dealt_seqs) if dealt_seqs else 0.0
        kills      = sum(1 for s in dealt_seqs if s["outcome"] == "kill")
        edgeguards = sum(1 for s in dealt_seqs if s["outcome"] == "edgeguard")
        resets     = sum(1 for s in dealt_seqs if s["outcome"] == "reset")

        eg_above_att  = sum(p["edgeguard"]["above"]["attempts"]   for p in pdata)
        eg_above_conv = sum(p["edgeguard"]["above"]["conversions"] for p in pdata)
        eg_below_att  = sum(p["edgeguard"]["below"]["attempts"]   for p in pdata)
        eg_below_conv = sum(p["edgeguard"]["below"]["conversions"] for p in pdata)

        wins = sum(1 for p in pdata if p["won"])

        def rate_str(suc, att):
            return f"{100.0*suc/att:.0f}%  ({suc}/{att})" if att > 0 else "N/A"

        def flag(rate, lo=70, hi=90):
            if rate is None: return ""
            if rate < lo: return "  [!]"
            if rate < hi: return "  [~]"
            return "  [ok]"

        lc_s = f"{lc_rate:.0f}%  ({total_lc_suc}/{total_lc_att})" if lc_rate is not None else "N/A"
        wd_s = f"{wd_rate:.0f}%  ({total_wd_prf}/{total_wd_att})" if wd_rate is not None else "N/A"
        f1_s = f"{f1_rate:.0f}%  ({total_f1_prf}/{total_f1_att})" if f1_rate is not None else "N/A"

        out(f"    Record            : {wins}-{n - wins}")
        out(f"    SDs / game        : {avg(lambda p: p.get('sd_count', 0)):.1f}")
        out(f"    Avg shield time   : {avg(lambda p: p['neutral']['shield_seconds']):.1f}s/game")
        out(f"    Avg crouch time   : {avg(lambda p: p['neutral']['crouch_seconds']):.1f}s/game")
        out(f"    Center stage      : {avg(lambda p: p['stage_control']['center_pct']):.1f}%{flag(avg(lambda p: p['stage_control']['center_pct']), 40, 60)}")
        out(f"    Aerials           : {total_high} high (autocancel) / {total_low} low (L-cancel window)")
        out(f"    L-cancel rate     : {lc_s}{flag(lc_rate)}")
        out(f"    Wavedash rate     : {wd_s}{flag(wd_rate)}")
        out(f"    Frame-1 aerials   : {f1_s}{flag(f1_rate)}")
        if lt_eng > 0:
            dwell_avg = lt_dwell / lt_dwn if lt_dwn else 0.0
            inv = f"{100.0 * lt_hinv / lt_hang:.0f}% invuln on ledge" if lt_hang > 0 else "invuln n/a"
            out(f"    Ledge tech        : {lt_eng} grabs, avg {dwell_avg:.0f}f to act, {inv}")
            if lt_ld > 0:
                galint = lt_gsum / lt_gn if lt_gn else 0.0
                gpct   = 100.0 * lt_gpos / lt_gn if lt_gn else 0.0
                react  = lt_react / lt_ld
                wland  = lt_wl / lt_ld
                fall   = (lt_fsum / lt_fn) if lt_fn else 0.0
                dist   = lt_dist / lt_gn if lt_gn else 0.0
                out(f"      Ledgedash       : {lt_ld} ledgedashes, GALINT avg {galint:.0f}f"
                    f" best {lt_gmax}f ({gpct:.0f}% keep invuln)")
                out(f"                        reaction {react:.0f}f, fall {fall:.0f}f, "
                    f"waveland {wland:.0f}f, dist {dist:.1f}")
            opt_str = ", ".join(f"{o} {lt_opts[o]}" for o in LEDGE_OPTIONS if lt_opts.get(o, 0) > 0)
            err = "  [!] ledge-jump = tech error" if lt_opts.get("ledge_jump_direct", 0) > 0 else ""
            out(f"      Options         : {opt_str}{err}")
        out(f"    Avg punish dealt  : {avg_punish_dealt:.1f}%  ({len(dealt_seqs)} sequences)")
        if dealt_seqs:
            out(f"    Punish outcomes   : {kills} kills / {edgeguards} edgeguards / {resets} resets  ({100*kills//len(dealt_seqs)}% kill rate)")
        out(f"    Edgeguard (above) : {rate_str(eg_above_conv, eg_above_att)}")
        out(f"    Edgeguard (below) : {rate_str(eg_below_conv, eg_below_att)}")
        out()

    if focus_port is not None:
        write_player_section(focus_port, label_prefix="YOU -- ")
        for port_idx in all_ports:
            if port_idx != focus_port:
                write_player_section(port_idx, label_prefix="OPPONENT -- ")
    else:
        for port_idx in all_ports:
            write_player_section(port_idx)

    # Flagged games
    out()
    out("=" * 70)
    out("  GAMES WORTH REVIEWING")
    out("=" * 70)
    out()

    flagged = []
    for gnum, g in enumerate(game_summaries, 1):
        reasons = []
        for port_idx in g["port_order"]:
            if focus_port is not None and port_idx != focus_port:
                continue
            p  = g["ports"][port_idx]
            ts = p["tech_skill"]
            pu = p["punishes"]
            nt = p["neutral"]
            sc = p["stage_control"]
            char = p["label"]

            if ts["l_cancel_attempts"] > 0 and ts["l_cancel_rate"] < 60:
                reasons.append(f"{char}: L-cancel {ts['l_cancel_rate']:.0f}%")
            if ts["wd_attempts"] > 5 and ts["wd_rate"] < 60:
                reasons.append(f"{char}: wavedash {ts['wd_rate']:.0f}%")
            if pu["count"] >= 5 and pu["avg_damage_dealt"] < 12:
                reasons.append(f"{char}: low avg punish ({pu['avg_damage_dealt']:.1f}%)")
            if nt["shield_seconds"] > 20:
                reasons.append(f"{char}: high shield time ({nt['shield_seconds']:.1f}s)")
            if sc["total_frames"] > 0 and sc["center_pct"] < 25:
                reasons.append(f"{char}: low center stage ({sc['center_pct']:.1f}%)")

        if reasons:
            flagged.append((gnum, g["file"], g["stage"], reasons))

    if flagged:
        for gnum, fname, stage, reasons in flagged:
            out(f"  Game {gnum} -- {fname} ({stage})")
            for r in reasons:
                out(f"    -> {r}")
            out()
    else:
        out("  No games flagged.")
        out()

    out("=" * 70)
    out("  END OF SET REPORT")
    out("=" * 70)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze a set of Slippi games vs the same opponent."
    )
    parser.add_argument("folder", help="Path to Slippi folder (or parent with YYYY-MM subfolders)")
    parser.add_argument("count",  type=int, nargs="?", default=None, help="Number of most recent games")
    parser.add_argument("--port", type=int, default=None, help="Your port (0-indexed)")
    parser.add_argument("--code", type=str, default=None, help="Your Slippi connect code (e.g. ABCD#123)")
    parser.add_argument("--out",  type=str, default=None, help="Write report to file")
    parser.add_argument("--files", nargs="+", metavar="FILE", help="Analyze specific .slp files")
    args = parser.parse_args()

    if args.files:
        report = set_report(args.folder, len(args.files), focus_port=args.port,
                            my_code=args.code, files=args.files)
    elif args.count:
        report = set_report(args.folder, args.count, focus_port=args.port, my_code=args.code)
    else:
        parser.error("provide a count or --files")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Report saved to {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
