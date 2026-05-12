#!/usr/bin/env python3
"""
Session Review
==============
Aggregates all games from a session, groups them into sets by opponent,
and shows per-set summaries plus overall session totals.

Usage:
    python session_review.py "C:/path/to/slippi" --code WAWI#755
    python session_review.py "C:/path/to/slippi" --code WAWI#755 --count 20
    python session_review.py "C:/path/to/slippi" --code WAWI#755 --out session.txt
"""

import sys
import os
import re
import argparse

from game_review import (
    analyze, detect_port, get_netplay_info, get_direct_codes,
    AERIALS, POSTLAND_CATEGORIES, POSTLAND_AERIAL_BUCKETS,
    _format_postland_categories,
)


def _opponent_from_filename(fname, my_name):
    """Extract opponent name from a tournament filename like:
    'N - PlayerA (Char), PlayerB (Char) - Stage.slp'
    Returns the first player name that isn't my_name, or None.
    """
    players = re.findall(r'([A-Za-z0-9_]+)\s*\([^)]+\)', fname)
    for name in players:
        if name.upper() != my_name.upper():
            return name
    return None

FPS = 60

# Pro replays are stored under this directory, organized as:
#   pro_replays/sheik_vs_falco/
#   pro_replays/sheik_vs_fox/
# etc.
PRO_REPLAYS_BASE = os.path.join(os.path.dirname(__file__), "pro_replays")


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


def get_all_slp_files(folder, count=None):
    resolved = resolve_folder(folder)
    files = [
        os.path.join(resolved, f)
        for f in os.listdir(resolved)
        if f.lower().endswith(".slp")
    ]
    files.sort(key=os.path.getmtime)
    if count:
        files = files[-count:]
    return files, resolved


def group_into_sets(game_summaries):
    """Group consecutive games against the same opponent into sets.
    Uses per-game my_port stored in game_data["my_port"]."""
    sets = []
    current_set = []
    current_opp = None

    for g in game_summaries:
        my_port  = g["my_port"]
        opp_ports = [p for p in g["port_order"] if p != my_port]
        opp_port  = opp_ports[0] if opp_ports else None
        opp_code  = g["ports"][opp_port].get("netplay_code", "") if opp_port is not None else ""

        if opp_code != current_opp:
            if current_set:
                sets.append(current_set)
            current_set = [g]
            current_opp = opp_code
        else:
            current_set.append(g)

    if current_set:
        sets.append(current_set)

    return sets


def aggregate_stats(game_summaries):
    """Compute aggregate stats across games, using per-game my_port."""
    if not game_summaries:
        return None

    pdata      = []
    dealt_seqs = []
    eg_above_att = eg_above_conv = eg_below_att = eg_below_conv = 0
    wins = 0

    for g in game_summaries:
        my_port = g["my_port"]
        if my_port not in g["ports"]:
            continue
        p = g["ports"][my_port]
        pdata.append(p)

        opp_ports = [pi for pi in g["port_order"] if pi != my_port]
        if opp_ports and opp_ports[0] in g["ports"]:
            dealt_seqs.extend(g["ports"][opp_ports[0]]["punishes"]["sequences"])

        eg_above_att  += p["edgeguard"]["above"]["attempts"]
        eg_above_conv += p["edgeguard"]["above"]["conversions"]
        eg_below_att  += p["edgeguard"]["below"]["attempts"]
        eg_below_conv += p["edgeguard"]["below"]["conversions"]
        if p["won"]:
            wins += 1

    if not pdata:
        return None

    n = len(pdata)

    total_high   = sum(p["tech_skill"]["high_aerials"]       for p in pdata)
    total_low    = sum(p["tech_skill"]["low_aerials"]        for p in pdata)
    total_lc_att = sum(p["tech_skill"]["l_cancel_attempts"] for p in pdata)
    total_lc_suc = sum(p["tech_skill"]["l_cancel_success"]  for p in pdata)
    total_wd_att = sum(p["tech_skill"]["wd_attempts"]       for p in pdata)
    total_wd_prf = sum(p["tech_skill"]["wd_perfect"]        for p in pdata)
    total_f1_att = sum(p["tech_skill"]["f1_attempts"]       for p in pdata)
    total_f1_prf = sum(p["tech_skill"]["f1_perfect"]        for p in pdata)

    # Per-aerial sums
    def _sum_by_aerial(key):
        return {a: sum(p["tech_skill"][key].get(a, 0) for p in pdata) for a in AERIALS}
    lc_att_by_aerial = _sum_by_aerial("lc_att_by_aerial")
    lc_suc_by_aerial = _sum_by_aerial("lc_suc_by_aerial")
    high_by_aerial   = _sum_by_aerial("high_by_aerial")
    low_by_aerial    = _sum_by_aerial("low_by_aerial")
    f1_att_by_aerial = _sum_by_aerial("f1_att_by_aerial")
    f1_prf_by_aerial = _sum_by_aerial("f1_prf_by_aerial")

    # Post-landing aggregation
    pl_samples    = sum(p["post_landing"]["samples"]           for p in pdata)
    pl_total_wait = sum(p["post_landing"]["total_wait_frames"] for p in pdata)
    pl_categories = {c: sum(p["post_landing"]["categories"].get(c, 0) for p in pdata)
                     for c in POSTLAND_CATEGORIES}
    pl_by_aerial = {}
    for a in POSTLAND_AERIAL_BUCKETS:
        bs  = sum(p["post_landing"]["by_aerial"].get(a, {}).get("samples", 0)           for p in pdata)
        btw = sum(p["post_landing"]["by_aerial"].get(a, {}).get("total_wait_frames", 0) for p in pdata)
        bc  = {c: sum(p["post_landing"]["by_aerial"].get(a, {}).get("categories", {}).get(c, 0)
                      for p in pdata) for c in POSTLAND_CATEGORIES}
        pl_by_aerial[a] = {"samples": bs, "total_wait_frames": btw, "categories": bc}
    post_landing = {
        "samples": pl_samples,
        "total_wait_frames": pl_total_wait,
        "avg_frames_to_act": (pl_total_wait / pl_samples) if pl_samples else 0.0,
        "categories": pl_categories,
        "by_aerial": pl_by_aerial,
    }

    def safe_pct(a, b): return 100.0 * a / b if b > 0 else None

    return {
        "games":          n,
        "wins":           wins,
        "losses":         n - wins,
        "avg_stocks_lost": sum(p["stocks_lost"] for p in pdata) / n,
        "avg_shield_s":   sum(p["neutral"]["shield_seconds"] for p in pdata) / n,
        "avg_crouch_s":   sum(p["neutral"]["crouch_seconds"] for p in pdata) / n,
        "avg_center_pct": sum(p["stage_control"]["center_pct"] for p in pdata) / n,
        "high_aerials":   total_high,
        "low_aerials":    total_low,
        "lc_att":         total_lc_att,
        "lc_suc":         total_lc_suc,
        "lc_rate":        safe_pct(total_lc_suc, total_lc_att),
        "wd_att":         total_wd_att,
        "wd_prf":         total_wd_prf,
        "wd_rate":        safe_pct(total_wd_prf, total_wd_att),
        "f1_att":         total_f1_att,
        "f1_prf":         total_f1_prf,
        "f1_rate":        safe_pct(total_f1_prf, total_f1_att),
        "lc_att_by_aerial": lc_att_by_aerial,
        "lc_suc_by_aerial": lc_suc_by_aerial,
        "high_by_aerial":   high_by_aerial,
        "low_by_aerial":    low_by_aerial,
        "f1_att_by_aerial": f1_att_by_aerial,
        "f1_prf_by_aerial": f1_prf_by_aerial,
        "post_landing":   post_landing,
        "dealt_seqs":     dealt_seqs,
        "avg_punish":     sum(s["damage"] for s in dealt_seqs) / len(dealt_seqs) if dealt_seqs else 0.0,
        "kills":          sum(1 for s in dealt_seqs if s["outcome"] == "kill"),
        "edgeguards":     sum(1 for s in dealt_seqs if s["outcome"] == "edgeguard"),
        "resets":         sum(1 for s in dealt_seqs if s["outcome"] == "reset"),
        "eg_above_att":   eg_above_att,
        "eg_above_conv":  eg_above_conv,
        "eg_below_att":   eg_below_att,
        "eg_below_conv":  eg_below_conv,
    }


def aggregate_stats_opponent(game_summaries):
    """Same as aggregate_stats but from the opponent's perspective."""
    opponent_games = []
    for g in game_summaries:
        my_port   = g["my_port"]
        opp_ports = [p for p in g["port_order"] if p != my_port]
        if not opp_ports:
            continue
        opp_port = opp_ports[0]
        # Swap my_port so aggregate_stats sees the opponent as "me"
        g_copy = dict(g)
        g_copy["my_port"] = opp_port
        opponent_games.append(g_copy)
    return aggregate_stats(opponent_games) if opponent_games else None


def _normalize_char(name):
    return name.lower().replace(" ", "_")


def pro_replays_dir(my_char, opp_char):
    """Return the expected pro replays directory for this matchup, or None if not found."""
    name = f"{_normalize_char(my_char)}_vs_{_normalize_char(opp_char)}"
    path = os.path.join(PRO_REPLAYS_BASE, name)
    return path if os.path.isdir(path) else None


def load_pro_stats(my_char, opp_char, stages=None):
    """Load and aggregate stats from pro replays for this matchup.

    Detects which port is my_char by character name.
    If stages is a set of strings, only includes games played on those stages.
    Returns (stats_dict, n_files_total) or (None, 0) if no directory found.
    """
    pro_dir = pro_replays_dir(my_char, opp_char)
    if pro_dir is None:
        return None, 0

    slp_files = [
        os.path.join(pro_dir, f)
        for f in os.listdir(pro_dir)
        if f.lower().endswith(".slp")
    ]
    if not slp_files:
        return None, 0

    game_summaries = []
    for path in slp_files:
        _, game_data = analyze(path)
        if game_data is None:
            continue

        # Find which port is my_char
        my_port = None
        for port_idx, pdata in game_data["ports"].items():
            if pdata["char"].lower() == my_char.lower():
                my_port = port_idx
                break
        if my_port is None:
            continue

        # Stage filter
        if stages and game_data.get("stage") not in stages:
            continue

        game_data["my_port"] = my_port
        game_summaries.append(game_data)

    if not game_summaries:
        return None, len(slp_files)

    return aggregate_stats(game_summaries), len(slp_files)


def write_stats_block(stats, out, indent="    "):
    """Write a compact stats block from an aggregate_stats() result."""
    def flag(rate, lo=70, hi=90):
        if rate is None: return ""
        if rate < lo: return "  [!]"
        if rate < hi: return "  [~]"
        return "  [ok]"

    def rate_str(suc, att):
        return f"{100.0*suc/att:.0f}%  ({suc}/{att})" if att > 0 else "N/A"

    lc_s = rate_str(stats["lc_suc"], stats["lc_att"])
    wd_s = rate_str(stats["wd_prf"], stats["wd_att"])
    f1_s = rate_str(stats["f1_prf"], stats["f1_att"])

    out(f"{indent}Avg stocks lost   : {stats['avg_stocks_lost']:.1f}")
    out(f"{indent}Avg shield time   : {stats['avg_shield_s']:.1f}s/game")
    out(f"{indent}Center stage      : {stats['avg_center_pct']:.1f}%{flag(stats['avg_center_pct'], 40, 60)}")
    out(f"{indent}Aerials           : {stats['high_aerials']} high / {stats['low_aerials']} low (L-cancel window)")
    out(f"{indent}L-cancel rate     : {lc_s}{flag(stats['lc_rate'])}")
    # Per-aerial L-cancel breakdown
    if stats["lc_att_by_aerial"] and any(stats["lc_att_by_aerial"].values()):
        for a in AERIALS:
            att = stats["lc_att_by_aerial"][a]
            high = stats["high_by_aerial"][a]
            if att == 0 and high == 0:
                continue
            if att > 0:
                suc = stats["lc_suc_by_aerial"][a]
                rate = 100.0 * suc / att
                suffix = f" (+{high} high)" if high > 0 else ""
                out(f"{indent}  {a:4s}            : {suc}/{att}  ({rate:.0f}%){flag(rate)}{suffix}")
            else:
                out(f"{indent}  {a:4s}            : {high} high (autocancel only)")
    out(f"{indent}Wavedash rate     : {wd_s}{flag(stats['wd_rate'])}")
    out(f"{indent}Frame-1 aerials   : {f1_s}{flag(stats['f1_rate'])}")
    # Per-aerial F1 breakdown
    if stats["f1_att_by_aerial"] and any(stats["f1_att_by_aerial"].values()):
        for a in AERIALS:
            att = stats["f1_att_by_aerial"][a]
            if att == 0:
                continue
            prf = stats["f1_prf_by_aerial"][a]
            rate = 100.0 * prf / att
            out(f"{indent}  {a:4s}            : {prf}/{att}  ({rate:.0f}%)")
    # Post-landing options
    pl = stats.get("post_landing")
    if pl and pl.get("samples", 0) > 0:
        out(f"{indent}Post-landing      : {pl['samples']} samples, avg {pl['avg_frames_to_act']:.1f}f to act")
        out(f"{indent}  {_format_postland_categories(pl['categories'], pl['samples'])}")
        for a in POSTLAND_AERIAL_BUCKETS:
            b = pl["by_aerial"].get(a, {})
            bs = b.get("samples", 0)
            if bs == 0:
                continue
            bavg = b["total_wait_frames"] / bs
            cats_str = _format_postland_categories(b["categories"], bs)
            out(f"{indent}  {a:5s} ({bs:4d}) avg {bavg:4.1f}f  {cats_str}")
    out(f"{indent}Avg punish dealt  : {stats['avg_punish']:.1f}%  ({len(stats['dealt_seqs'])} sequences)")
    if stats["dealt_seqs"]:
        total = len(stats["dealt_seqs"])
        out(f"{indent}Punish outcomes   : {stats['kills']} kills / {stats['edgeguards']} edgeguards / {stats['resets']} resets  ({100*stats['kills']//total}% kill rate)")
    out(f"{indent}Edgeguard (above) : {rate_str(stats['eg_above_conv'], stats['eg_above_att'])}")
    out(f"{indent}Edgeguard (below) : {rate_str(stats['eg_below_conv'], stats['eg_below_att'])}")


LOSER_LABELS = {
    "whiffed":             "Whiffed a move",
    "airdodged":           "Airdodged",
    "landing_lag":         "Landing lag",
    "attacked_into_shield":"Attacked into shield (grabbed OOS)",
    "attacked_cc_grabbed": "Attacked (CC'd & grabbed)",
    "grabbed_neutral":     "Grabbed from neutral",
    "caught_neutral":      "Caught in neutral",
    "missed_tech":         "Missed tech / wakeup",
    "reversal_victim":     "Got reversal'd (extending punish)",
    "unknown":             "Other",
}
WINNER_LABELS = {
    "whiff_punish":    "Whiff punish",
    "airdodge_punish": "Airdodge punish",
    "landing_punish":  "Landing punish",
    "oos_grab":        "OOS grab",
    "cc_grab":         "CC grab",
    "dash_grab":       "Dash grab",
    "walk_grab":       "Walk-up grab",
    "aerial_approach": "Aerial approach",
    "dash_attack":     "Dash attack",
    "ground_attack":   "Grounded attack",
    "approach":        "Approach / other",
    "tech_punish":     "Tech punish",
    "reversal_winner": "Reversal",
    "unknown":         "Other",
}


def _merge_neutral_counts(game_summaries, my_port_key="my_port"):
    """Aggregate neutral win/loss counts across a list of game summaries."""
    neutral_wins   = 0
    neutral_losses = 0
    continuations  = 0
    win_by         = {}
    loss_by        = {}

    for g in game_summaries:
        my_port   = g["my_port"]
        opp_ports = [p for p in g["port_order"] if p != my_port]
        if not opp_ports:
            continue
        opp_port = opp_ports[0]

        my_p  = g["ports"].get(my_port, {}).get("punishes", {})
        opp_p = g["ports"].get(opp_port, {}).get("punishes", {})

        neutral_wins   += my_p.get("neutral_wins", 0)
        neutral_losses += my_p.get("neutral_losses", 0)
        continuations  += my_p.get("continuations", 0)

        for k, v in my_p.get("neutral_win_by", {}).items():
            win_by[k] = win_by.get(k, 0) + v
        for k, v in my_p.get("neutral_loss_by", {}).items():
            loss_by[k] = loss_by.get(k, 0) + v

    return neutral_wins, neutral_losses, continuations, win_by, loss_by


def write_neutral_block(game_summaries, out, indent="  "):
    """Write a NEUTRAL ANALYSIS block aggregated across game_summaries."""
    wins, losses, conts, win_by, loss_by = _merge_neutral_counts(game_summaries)
    total = wins + losses + conts
    if total == 0:
        return

    out(f"{indent}NEUTRAL ANALYSIS")
    out("  " + "-" * 68)

    # You opened neutral
    out(f"{indent}Neutral wins — you opened ({wins}):")
    if win_by:
        for key in sorted(win_by, key=win_by.get, reverse=True):
            label = WINNER_LABELS.get(key, key)
            out(f"{indent}  {label:<38}: {win_by[key]}")
    else:
        out(f"{indent}  (none)")
    out()

    # Opponent opened neutral — what you were doing
    out(f"{indent}Neutral wins — opp opened ({losses}), you did:")
    if loss_by:
        for key in sorted(loss_by, key=loss_by.get, reverse=True):
            label = LOSER_LABELS.get(key, key)
            out(f"{indent}  {label:<38}: {loss_by[key]}")
    else:
        out(f"{indent}  (none)")
    out()

    if conts:
        out(f"{indent}Punish continuations (tech situations): {conts}")
        out()


def session_report(folder, my_code, count=None, sets=None):
    files, resolved = get_all_slp_files(folder, count)
    if not files:
        print(f"No .slp files found in: {resolved}")
        sys.exit(1)

    # When limiting by sets, scan newest-first so we can stop early,
    # then reverse to restore chronological order.
    scan_files = list(reversed(files)) if sets else files

    my_port = None
    game_summaries = []
    skipped = []
    set_count = 0
    current_opp = None  # track opponent changes to count sets

    for path in scan_files:
        port = detect_port(path, my_code)
        if port is None:
            skipped.append(os.path.basename(path))
            continue
        if my_port is None:
            my_port = port

        _, game_data = analyze(path, focus_port=port)
        if game_data is None:
            skipped.append(os.path.basename(path))
            continue
        game_data["file"] = os.path.basename(path)
        game_data["my_port"] = port

        # For tournament files (no netplay codes), patch opponent code from filename
        my_name = my_code.split("#")[0]
        opp_ports_tmp = [p for p in game_data["port_order"] if p != port]
        if opp_ports_tmp:
            opp_p = opp_ports_tmp[0]
            if not game_data["ports"][opp_p].get("netplay_code"):
                opp_name = _opponent_from_filename(os.path.basename(path), my_name)
                if opp_name:
                    game_data["ports"][opp_p]["netplay_code"] = opp_name

        # Count set transitions (newest-first when sets limit is active)
        opp_ports = [p for p in game_data["port_order"] if p != port]
        opp_code  = game_data["ports"][opp_ports[0]].get("netplay_code", "") if opp_ports else ""
        if sets and opp_code != current_opp:
            if set_count >= sets:
                break
            set_count += 1
            current_opp = opp_code

        game_summaries.append(game_data)

    if sets:
        game_summaries.reverse()

    lines = []
    def out(s=""): lines.append(s)

    out("=" * 70)
    out("  SESSION REVIEW")
    out("=" * 70)
    out(f"  Folder : {resolved}")
    out(f"  Code   : {my_code}")
    if not game_summaries:
        out("  No games found for this connect code.")
        return "\n".join(lines)

    direct_codes = get_direct_codes()
    sets = group_into_sets(game_summaries)
    total_games = len(game_summaries)
    session_stats = aggregate_stats(game_summaries)
    set_wins = sum(
        1 for s in sets
        if aggregate_stats(s)["wins"] > aggregate_stats(s)["losses"]
    )

    my_char = game_summaries[0]["ports"][my_port]["char"]

    out(f"  Games  : {total_games}  |  Sets: {len(sets)}  |  Character: {my_char}")
    if skipped:
        out(f"  Skipped: {len(skipped)} games (no matching connect code)")
    out()

    # Set index
    out("-" * 70)
    out(f"  {'#':<4} {'Opponent':<16} {'Char':<10} {'Record':<8} Stages")
    out("-" * 70)
    for i, set_games in enumerate(sets, 1):
        g0        = set_games[0]
        mp        = g0["my_port"]
        opp_ports = [p for p in g0["port_order"] if p != mp]
        if not opp_ports:
            continue
        opp_port = opp_ports[0]
        opp_data = g0["ports"].get(opp_port, {})
        opp_code = opp_data.get("netplay_code", "Unknown")
        opp_char = opp_data.get("char", "?")
        st  = aggregate_stats(set_games)
        rec = f"{st['wins']}-{st['losses']}"
        stages = " / ".join(g["stage"][:10] for g in set_games)
        out(f"  {i:<4} {opp_code:<16} {opp_char:<10} {rec:<8} {stages}")
    out()

    # Per-set detail
    for i, set_games in enumerate(sets, 1):
        g0        = set_games[0]
        mp        = g0["my_port"]
        opp_ports = [p for p in g0["port_order"] if p != mp]
        if not opp_ports:
            continue
        opp_port  = opp_ports[0]
        opp_data  = g0["ports"].get(opp_port, {})
        opp_code  = opp_data.get("netplay_code", "Unknown")
        opp_char  = opp_data.get("char", "?")
        st        = aggregate_stats(set_games)
        result    = "W" if st["wins"] > st["losses"] else "L"

        out("=" * 70)
        out(f"  SET {i}  vs {opp_code} ({opp_char})   {result} {st['wins']}-{st['losses']}")
        out("=" * 70)
        out()

        # Per-game rows
        out(f"  {'#':<4} {'Stage':<22} {'Result':<7} {'AvgPun':>7} {'L-cnc':>7} {'WD%':>6} {'F1%':>6} {'Ctr%':>6}")
        out("  " + "-" * 64)
        for gnum, g in enumerate(set_games, 1):
            my_port = g["my_port"]
            if my_port not in g["ports"]:
                continue
            p  = g["ports"][my_port]
            ts = p["tech_skill"]
            sc = p["stage_control"]
            pu = p["punishes"]
            l_rate  = f"{ts['l_cancel_rate']:.0f}%" if ts["l_cancel_attempts"] > 0 else "N/A"
            wd_rate = f"{ts['wd_rate']:.0f}%"       if ts["wd_attempts"] > 0       else "N/A"
            f1_rate = f"{ts['f1_rate']:.0f}%"       if ts["f1_attempts"] > 0       else "N/A"
            result_str = f"W {p['start_stocks'] - p['stocks_lost']}-0" if p["won"] else f"L 0-?"
            out(f"  {gnum:<4} {g['stage'][:20]:<22} {result_str:<7} {pu['avg_damage_dealt']:>7.1f} "
                f"{l_rate:>7} {wd_rate:>6} {f1_rate:>6} {sc['center_pct']:>5.1f}%")
        out()

        my_char_set = g0["ports"].get(mp, {}).get("char", my_char)
        set_stages  = {g["stage"] for g in set_games}

        out(f"  YOU ({my_char_set})")
        out("-" * 70)
        write_stats_block(st, out)
        out()
        write_neutral_block(set_games, out)

        # Pro comparison
        pro_stats, pro_total = load_pro_stats(my_char_set, opp_char, stages=set_stages)
        pro_dir = pro_replays_dir(my_char_set, opp_char)
        if pro_dir is None:
            out(f"  [no pro replays for {my_char_set} vs {opp_char} — run /fetch-pro-replays to download]")
            out()
        elif pro_stats is None:
            stage_str = ", ".join(sorted(set_stages))
            out(f"  [pro replays found ({pro_total} files) but none on: {stage_str}]")
            out()
        else:
            stage_str = ", ".join(sorted(set_stages))
            out(f"  PRO BASELINE ({my_char_set} vs {opp_char} — {pro_stats['games']} games on {stage_str})")
            out("-" * 70)
            write_stats_block(pro_stats, out)
            out()

        if opp_code.upper() in direct_codes:
            opp_st = aggregate_stats_opponent(set_games)
            if opp_st:
                out(f"  {opp_code} ({opp_char})")
                out("-" * 70)
                write_stats_block(opp_st, out)
                out()

    # Session totals
    out("=" * 70)
    out("  SESSION TOTALS")
    out("=" * 70)
    out()
    out(f"    Sets record       : {set_wins}-{len(sets) - set_wins}")
    out(f"    Games record      : {session_stats['wins']}-{session_stats['losses']}")
    out()
    write_stats_block(session_stats, out)
    out()

    out("=" * 70)
    out("  END OF SESSION REPORT")
    out("=" * 70)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Aggregate session stats across multiple sets."
    )
    parser.add_argument("folder", help="Path to Slippi folder (or parent with YYYY-MM subfolders)")
    parser.add_argument("--code",  type=str, required=True, help="Your Slippi connect code (e.g. WAWI#755)")
    parser.add_argument("--sets",  type=int, default=None,  help="Number of most recent sets to include")
    parser.add_argument("--count", type=int, default=None,  help="Max number of recent games to include")
    parser.add_argument("--out",   type=str, default=None,  help="Write report to file")
    args = parser.parse_args()

    report = session_report(args.folder, args.code, count=args.count, sets=args.sets)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Report saved to {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
