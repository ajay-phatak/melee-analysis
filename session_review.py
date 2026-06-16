#!/usr/bin/env python3
"""
Session Review
==============
Aggregates all games from a session, groups them into sets by matchup
(opponent + characters), and shows per-set summaries plus overall totals.

Usage:
    python session_review.py "C:/path/to/slippi" --code ABCD#123
    python session_review.py "C:/path/to/slippi" --code ABCD#123 --count 20
    python session_review.py "C:/path/to/slippi" --code ABCD#123 --out session.txt
"""

import sys
import os
import re
import json
import pickle
import datetime
import argparse

from game_review import (
    analyze, detect_port, get_netplay_info, get_direct_codes,
    AERIALS, POSTLAND_CATEGORIES, POSTLAND_AERIAL_BUCKETS,
    LEDGE_OPTIONS, LEDGE_INTANG_FRAMES,
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


def _matchup_key(g):
    """Identity of a matchup: (opponent code, my character, opponent character).
    A new set begins whenever any of these change, so the same opponent
    switching characters (or the player switching) splits into separate sets.
    Uses per-game my_port stored in game_data["my_port"]."""
    my_port   = g["my_port"]
    my_char   = g["ports"].get(my_port, {}).get("char", "")
    opp_ports = [p for p in g["port_order"] if p != my_port]
    opp_port  = opp_ports[0] if opp_ports else None
    opp_code  = g["ports"][opp_port].get("netplay_code", "") if opp_port is not None else ""
    opp_char  = g["ports"][opp_port].get("char", "") if opp_port is not None else ""
    return (opp_code, my_char, opp_char)


def group_into_sets(game_summaries, pool=False):
    """Group games into matchup sets, keyed by (opponent, my char, opp char).

    Default: a set is a run of *consecutive* games of one matchup, so the same
    matchup played in two blocks (e.g. Falco, then Fox, then Falco again) yields
    two separate Falco sets.

    pool=True: all games of a matchup are pooled into a single set regardless of
    order, so the two Falco blocks above merge into one. Pooled sets are ordered
    by first appearance.

    Uses per-game my_port stored in game_data["my_port"]."""
    if pool:
        groups = {}
        order = []
        for g in game_summaries:
            key = _matchup_key(g)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(g)
        return [groups[k] for k in order]

    sets = []
    current_set = []
    current_key = None

    for g in game_summaries:
        key = _matchup_key(g)
        if key != current_key:
            if current_set:
                sets.append(current_set)
            current_set = [g]
            current_key = key
        else:
            current_set.append(g)

    if current_set:
        sets.append(current_set)

    return sets


# --- Gameplan distribution helpers (matchup-level neutral/punish flow) --------
def _move_dist(seqs, key):
    """Count seqs by a move/context key (None -> 'other')."""
    d = {}
    for s in seqs:
        m = s.get(key) or "other"
        d[m] = d.get(m, 0) + 1
    return d


def _opened_by(recv_seqs):
    """How you get opened: 'their move | your mistake' -> count."""
    d = {}
    for s in recv_seqs:
        move = s.get("opener_move") or "other"
        mistake = s.get("loser_context", "unknown")
        k = f"{move}|{mistake}"
        d[k] = d.get(k, 0) + 1
    return d


def _ender_outcomes(seqs):
    """Your ender move -> {kill/edgeguard/reset: count}."""
    d = {}
    for s in seqs:
        m = s.get("ender_move") or "other"
        o = s.get("outcome", "reset")
        slot = d.setdefault(m, {"kill": 0, "edgeguard": 0, "reset": 0})
        slot[o] = slot.get(o, 0) + 1
    return d


def _kill_moves(seqs):
    """Moves that secured a kill -> count."""
    d = {}
    for s in seqs:
        if s.get("outcome") == "kill":
            m = s.get("ender_move") or "other"
            d[m] = d.get(m, 0) + 1
    return d


# Percent buckets for percent-aware punish analysis, keyed by the victim's
# percent when the opening happened (start_pct of the sequence).
PCT_BUCKETS = ((0, 35, "0-34"), (35, 80, "35-79"), (80, 120, "80-119"), (120, 10**9, "120+"))
PCT_BUCKET_ORDER = tuple(label for _, _, label in PCT_BUCKETS)


def _pct_bucket(p):
    for lo, hi, label in PCT_BUCKETS:
        if lo <= p < hi:
            return label
    return PCT_BUCKET_ORDER[-1]


def _string_by_pct(seqs):
    """Start-percent bucket -> outcome counts + damage (conversion by %)."""
    d = {}
    for s in seqs:
        sp = s.get("start_pct")
        if sp is None:
            continue
        b = d.setdefault(_pct_bucket(sp),
                         {"n": 0, "kill": 0, "edgeguard": 0, "reset": 0, "dmg_sum": 0.0})
        b["n"] += 1
        o = s.get("outcome", "reset")
        b[o] = b.get(o, 0) + 1
        b["dmg_sum"] += s.get("damage", 0.0)
    return d


def _kill_pcts(seqs):
    """Killing move -> {n, sum_pct}: the percent victims die at, per move."""
    d = {}
    for s in seqs:
        if s.get("outcome") != "kill":
            continue
        ep = s.get("end_pct")
        if ep is None:
            continue
        m = s.get("ender_move") or "other"
        slot = d.setdefault(m, {"n": 0, "sum_pct": 0.0})
        slot["n"] += 1
        slot["sum_pct"] += ep
    return d


def _followups(seqs):
    """'opener_move|pct_bucket' -> {second_hit_move_or_'end': count}.
    The punish tree: given an opener at a percent range, what came next
    ('end' = the string stopped after the opening hit)."""
    d = {}
    for s in seqs:
        sp = s.get("start_pct")
        if sp is None:
            continue
        opener = s.get("opener_move") or "other"
        key = f"{opener}|{_pct_bucket(sp)}"
        hits = s.get("hit_moves") or []
        nxt = (hits[1][0] or "other") if len(hits) >= 2 else "end"
        slot = d.setdefault(key, {})
        slot[nxt] = slot.get(nxt, 0) + 1
    return d


def _reversal_summary(recv_seqs):
    """Reversals against you: an ill-conceived combo extension / edgeguard
    attempt that became the opponent's opening, with its cost."""
    out = {"n": 0, "stocks": 0, "dmg_sum": 0.0, "pct_sum": 0.0,
           "kinds": {}, "moves": {}}
    for s in recv_seqs:
        if s.get("loser_context") != "reversal_victim":
            continue
        out["n"] += 1
        out["dmg_sum"] += s.get("damage", 0.0)
        out["pct_sum"] += s.get("start_pct") or 0.0
        if s.get("outcome") == "kill":
            out["stocks"] += 1
        k = s.get("reversal_kind") or "unknown"
        out["kinds"][k] = out["kinds"].get(k, 0) + 1
        m = s.get("loser_move")
        if m:
            out["moves"][m] = out["moves"].get(m, 0) + 1
    return out


def _punished_moves(recv_seqs):
    """Your own move that created the opponent's opening -> count."""
    d = {}
    for s in recv_seqs:
        m = s.get("loser_move")
        if m:
            d[m] = d.get(m, 0) + 1
    return d


def aggregate_stats(game_summaries):
    """Compute aggregate stats across games, using per-game my_port."""
    if not game_summaries:
        return None

    pdata      = []
    dealt_seqs = []   # my punishes (opener/ender = mine)
    recv_seqs  = []   # punishes on me (how I get opened, their kill moves)
    eg_above_att = eg_above_conv = eg_below_att = eg_below_conv = 0
    eg_challenged = eg_free = 0
    eg_finishers = {}     # what ends my converted edgeguards
    oos_samples = oos_resolved = oos_wait = 0
    oos_categories = {}
    ledge_coverage = {}   # opponent ledge option -> {n, punished}
    sds = 0
    recov_att = recov_deaths = 0
    death_geo = {}    # my deaths by bucket
    kill_geo  = {}    # opponent deaths by bucket (how I kill them)
    center_win = []   # my center% in games I won
    center_loss = []  # my center% in games I lost
    wins = 0

    for g in game_summaries:
        my_port = g["my_port"]
        if my_port not in g["ports"]:
            continue
        p = g["ports"][my_port]
        pdata.append(p)
        recv_seqs.extend(p["punishes"]["sequences"])
        sds += p.get("sd_count", 0)
        recov_att    += p.get("recovery", {}).get("attempts", 0)
        recov_deaths += p.get("recovery", {}).get("deaths", 0)
        for b in p.get("death_buckets", []):
            death_geo[b] = death_geo.get(b, 0) + 1
        (center_win if p["won"] else center_loss).append(p["stage_control"]["center_pct"])

        opp_ports = [pi for pi in g["port_order"] if pi != my_port]
        if opp_ports and opp_ports[0] in g["ports"]:
            opp = g["ports"][opp_ports[0]]
            dealt_seqs.extend(opp["punishes"]["sequences"])
            for b in opp.get("death_buckets", []):
                kill_geo[b] = kill_geo.get(b, 0) + 1

        eg_above_att  += p["edgeguard"]["above"]["attempts"]
        eg_above_conv += p["edgeguard"]["above"]["conversions"]
        eg_below_att  += p["edgeguard"]["below"]["attempts"]
        eg_below_conv += p["edgeguard"]["below"]["conversions"]
        eg_challenged += p["edgeguard"].get("challenged", 0)
        eg_free       += p["edgeguard"].get("free", 0)
        for m, c in (p["edgeguard"].get("finish_moves") or {}).items():
            eg_finishers[m] = eg_finishers.get(m, 0) + c

        oo = p.get("oos") or {}
        oos_samples  += oo.get("samples", 0)
        oos_resolved += oo.get("resolved", 0)
        oos_wait     += oo.get("total_wait", 0)
        for c, v in (oo.get("categories") or {}).items():
            oos_categories[c] = oos_categories.get(c, 0) + v

        for opt, s in (p.get("ledge_coverage") or {}).items():
            slot = ledge_coverage.setdefault(opt, {"n": 0, "punished": 0})
            slot["n"] += s.get("n", 0)
            slot["punished"] += s.get("punished", 0)

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

    # Move-safety aggregation: sum each move's stat dict across games
    move_usage = {}
    for p in pdata:
        for m, s in (p.get("move_usage") or {}).items():
            slot = move_usage.setdefault(m, {})
            for k, v in s.items():
                slot[k] = slot.get(k, 0) + v

    # Ledge-tech aggregation (sum raw counts across games)
    def _lt_sum(key):
        return sum(p["ledge_tech"].get(key, 0) for p in pdata if "ledge_tech" in p)
    lt_option_counts = {
        o: sum(p["ledge_tech"]["option_counts"].get(o, 0)
               for p in pdata if "ledge_tech" in p)
        for o in LEDGE_OPTIONS
    }
    ledge_tech = {
        "engagements":        _lt_sum("engagements"),
        "option_counts":      lt_option_counts,
        "hang_frames":        _lt_sum("hang_frames"),
        "hang_invuln_frames": _lt_sum("hang_invuln_frames"),
        "dwell_frames":       _lt_sum("dwell_frames"),
        "dwell_n":            _lt_sum("dwell_n"),
        "ledgedash_count":    _lt_sum("ledgedash_count"),
        "galint_sum":         _lt_sum("galint_sum"),
        "galint_n":           _lt_sum("galint_n"),
        "galint_max":         max((p["ledge_tech"].get("galint_max", 0)
                                   for p in pdata if "ledge_tech" in p), default=0),
        "galint_pos":         _lt_sum("galint_pos"),
        "ld_reaction_sum":    _lt_sum("ld_reaction_sum"),
        "ld_fall_sum":        _lt_sum("ld_fall_sum"),
        "ld_fall_n":          _lt_sum("ld_fall_n"),
        "ld_waveland_sum":    _lt_sum("ld_waveland_sum"),
        "ld_distance_sum":    sum(p["ledge_tech"].get("ld_distance_sum", 0.0)
                                  for p in pdata if "ledge_tech" in p),
    }

    def safe_pct(a, b): return 100.0 * a / b if b > 0 else None

    return {
        "games":          n,
        "wins":           wins,
        "ledge_tech":     ledge_tech,
        "losses":         n - wins,
        "avg_stocks_lost": sum(p["stocks_lost"] for p in pdata) / n,
        "avg_sds":        sds / n,
        "recv_seqs":      recv_seqs,
        "avg_punish_against": (sum(s["damage"] for s in recv_seqs) / len(recv_seqs)
                               if recv_seqs else 0.0),
        # Gameplan distributions (matchup-level)
        "opened_by":        _opened_by(recv_seqs),
        "opening_sources":  _move_dist(dealt_seqs, "winner_context"),
        "opening_moves":    _move_dist(dealt_seqs, "opener_move"),
        "string_outcomes":  _ender_outcomes(dealt_seqs),
        "your_kill_moves":  _kill_moves(dealt_seqs),
        "their_kill_moves": _kill_moves(recv_seqs),
        # Percent-aware punish analysis (your strings, bucketed by start %)
        "string_by_pct":    _string_by_pct(dealt_seqs),
        "followups":        _followups(dealt_seqs),
        "kill_pcts":        _kill_pcts(dealt_seqs),
        "their_kill_pcts":  _kill_pcts(recv_seqs),
        # Reversal ledger + which of your own moves get punished
        "reversals":        _reversal_summary(recv_seqs),
        "punished_moves":   _punished_moves(recv_seqs),
        # Per-move outcome/punished/spacing profile (normals only)
        "move_usage":       move_usage,
        "death_geo":        death_geo,
        "kill_geo":         kill_geo,
        "recovery_att":     recov_att,
        "recovery_deaths":  recov_deaths,
        "center_win":       (sum(center_win) / len(center_win)) if center_win else None,
        "center_loss":      (sum(center_loss) / len(center_loss)) if center_loss else None,
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
        "eg_challenged":  eg_challenged,
        "eg_free":        eg_free,
        "eg_finishers":   eg_finishers,
        "oos": {
            "samples":    oos_samples,
            "resolved":   oos_resolved,
            "total_wait": oos_wait,
            "categories": oos_categories,
        },
        "ledge_coverage": ledge_coverage,
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


_SLP_DATE_RE = re.compile(r"(\d{8})T(\d{6})")


def _date_from_file(fname):
    """Parse YYYY-MM-DD from a Slippi filename like Game_20260605T223206.slp."""
    m = _SLP_DATE_RE.search(fname or "")
    if not m:
        return ""
    d = m.group(1)
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


def _r(x, nd=1):
    """Round, passing through None."""
    return round(x, nd) if x is not None else None


def _avg_kill_pct(kill_pcts):
    """Overall average kill percent from a {move: {n, sum_pct}} dict."""
    n = sum(v["n"] for v in kill_pcts.values())
    return round(sum(v["sum_pct"] for v in kill_pcts.values()) / n, 1) if n else None


def _oos_punish_pct(oos):
    """% of resolved shield-hit responses that were offensive options
    (grab / usmash / jump-OOS / platform shield-drop)."""
    res = oos.get("resolved", 0)
    if not res:
        return None
    cats = oos.get("categories") or {}
    off = sum(cats.get(c, 0) for c in ("grab", "usmash", "jump", "shielddrop"))
    return round(100.0 * off / res, 1)


def _whiff_metrics(move_usage):
    """(whiff_pct, whiff_punished_pct) across all normals, or Nones."""
    uses    = sum(s.get("n", 0) for s in move_usage.values())
    whiffs  = sum(s.get("whiff", 0) for s in move_usage.values())
    pun_wf  = sum(s.get("punished_whiff", 0) for s in move_usage.values())
    whiff_pct = round(100.0 * whiffs / uses, 1) if uses else None
    pun_pct   = round(100.0 * pun_wf / whiffs, 1) if whiffs else None
    return whiff_pct, pun_pct


def _set_record(set_games):
    """Flatten one matchup-set into a JSON-friendly record for the long-term coach.
    Reuses aggregate_stats (you) and aggregate_stats_opponent (neutral lost)."""
    g0 = set_games[0]
    mp = g0["my_port"]
    opp_ports = [p for p in g0["port_order"] if p != mp]
    opp_port = opp_ports[0] if opp_ports else None
    opp_data = g0["ports"].get(opp_port, {}) if opp_port is not None else {}

    st = aggregate_stats(set_games)
    opp_st = aggregate_stats_opponent(set_games)
    lt = st["ledge_tech"]
    files = sorted(g.get("file", "") for g in set_games)
    whiff_pct, whiff_punished_pct = _whiff_metrics(st["move_usage"])

    n_opened = len(st["dealt_seqs"])
    n_lost = len(opp_st["dealt_seqs"]) if opp_st else 0

    def pct(a, b):
        return round(100.0 * a / b, 1) if b else None

    return {
        "session_date": _date_from_file(files[0] if files else ""),
        "opp_code": opp_data.get("netplay_code", "Unknown"),
        "my_char": g0["ports"][mp]["char"],
        "opp_char": opp_data.get("char", "?"),
        "stages": [g["stage"] for g in set_games],
        "n_games": st["games"],
        "wins": st["wins"],
        "losses": st["losses"],
        "files": files,
        "metrics": {
            "sd_per_game": _r(st["avg_sds"], 2),
            "shield_s": _r(st["avg_shield_s"]),
            "crouch_s": _r(st["avg_crouch_s"]),
            "center_pct": _r(st["avg_center_pct"]),
            "lcancel_pct": _r(st["lc_rate"]),
            "wavedash_pct": _r(st["wd_rate"]),
            "f1_pct": _r(st["f1_rate"]),
            "punish_pct": _r(st["avg_punish"]),
            "kills": st["kills"],
            "kill_rate_pct": pct(st["kills"], n_opened),
            "edgeguard_above_pct": pct(st["eg_above_conv"], st["eg_above_att"]),
            "edgeguard_below_pct": pct(st["eg_below_conv"], st["eg_below_att"]),
            "neutral_opened": n_opened,
            "neutral_lost": n_lost,
            "neutral_win_pct": pct(n_opened, n_opened + n_lost),
            "ledgedash_count": lt["ledgedash_count"],
            "galint_avg": _r(lt["galint_sum"] / lt["galint_n"]) if lt["galint_n"] else None,
            "galint_best": lt["galint_max"],
            "galint_keep_pct": pct(lt["galint_pos"], lt["galint_n"]),
            "ledgedash_fall_avg": _r(lt["ld_fall_sum"] / lt["ld_fall_n"]) if lt["ld_fall_n"] else None,
            "ledge_hang_invuln_pct": pct(lt["hang_invuln_frames"], lt["hang_frames"]),
            "avg_kill_pct": _avg_kill_pct(st["kill_pcts"]),
            "reversals_per_game": _r(st["reversals"]["n"] / st["games"], 2),
            "whiff_pct": whiff_pct,
            "whiff_punished_pct": whiff_punished_pct,
            "oos_punish_pct": _oos_punish_pct(st["oos"]),
            "free_recovery_given_pct": pct(
                st["eg_free"], st["eg_above_att"] + st["eg_below_att"]),
        },
        # Matchup gameplan distributions — merged per-matchup over time by coach.py.
        "gameplan": {
            "opened_by":        st["opened_by"],
            "opening_sources":  st["opening_sources"],
            "string_outcomes":  st["string_outcomes"],
            "your_kill_moves":  st["your_kill_moves"],
            "their_kill_moves": st["their_kill_moves"],
            "death_geo":        st["death_geo"],
            "kill_geo":         st["kill_geo"],
            "recovery_att":     st["recovery_att"],
            "recovery_deaths":  st["recovery_deaths"],
            "dmg_per_opening_for":     _r(st["avg_punish"]),
            "dmg_per_opening_against": _r(st["avg_punish_against"]),
            "neutral_for":      n_opened,
            "neutral_against":  n_lost,
            "center_win":       _r(st["center_win"]) if st["center_win"] is not None else None,
            "center_loss":      _r(st["center_loss"]) if st["center_loss"] is not None else None,
            "string_by_pct":    st["string_by_pct"],
            "followups":        st["followups"],
            "kill_pcts":        st["kill_pcts"],
            "their_kill_pcts":  st["their_kill_pcts"],
            "reversals":        st["reversals"],
            "punished_moves":   st["punished_moves"],
            "move_usage":       st["move_usage"],
            "oos_samples":      st["oos"]["samples"],
            "oos_resolved":     st["oos"]["resolved"],
            "oos_wait":         st["oos"]["total_wait"],
            "oos_categories":   st["oos"]["categories"],
            "eg_challenged":    st["eg_challenged"],
            "eg_free":          st["eg_free"],
            "eg_att":           st["eg_above_att"] + st["eg_below_att"],
            "eg_finishers":     st["eg_finishers"],
            "ledge_coverage":   st["ledge_coverage"],
        },
    }


def build_json_payload(sets):
    """Structured per-set records for the whole session (consumed by coach.py)."""
    return {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "sets": [_set_record(s) for s in sets],
    }


def _normalize_char(name):
    return name.lower().replace(" ", "_")


def pro_replays_dir(my_char, opp_char):
    """Return the expected pro replays directory for this matchup, or None if not found."""
    name = f"{_normalize_char(my_char)}_vs_{_normalize_char(opp_char)}"
    path = os.path.join(PRO_REPLAYS_BASE, name)
    return path if os.path.isdir(path) else None


# Bump when build_data's per-game structure changes, to invalidate old caches.
# v2: added sd_count, death_buckets, recovery, opener_move/ender_move on punishes.
# v3: punish tracker now captures throws + tech-chases (dthrow strings).
# v4: start/end percent + per-hit move log on punishes; loser_move/reversal_kind.
# v5: hit_moves also logs mid-hitstun hits (true combos) via the damage-rise edge.
# v6: move_usage (per-move outcome / punished-rate / startup-distance profile).
# v7: oos (out-of-shield response), edgeguard challenged/free/finish_moves,
#     ledge_coverage (opponent ledge options vs my punish openings).
# v8: edgeguard finisher seeds from the launching move (edgehog was inflated
#     by knockback KOs whose killing blow landed before the ledge line).
# v9: deaths read at Dead-action-state entry instead of the stock decrement —
#     credits top/uair punish-kills (were closing as resets), derives kill
#     geography from the death state (DeadUp*->top, DeadLeft/Right->side,
#     DeadDown->edgehog), and re-grounds SD detection on the edgeguard tracker's
#     per-trip context (top star-KOs no longer mislabeled as self-destructs).
PRO_CACHE_VERSION = 9
PRO_CACHE_FILENAME = ".pro_cache.pkl"


def _pro_dir_signature(slp_files):
    """Fingerprint of the matchup's replay set: {filename: mtime}.
    Any add/remove/modify invalidates the cache."""
    return {os.path.basename(p): round(os.path.getmtime(p), 3) for p in slp_files}


def _load_pro_games(pro_dir, my_char, slp_files):
    """Parse all pro replays for a matchup into game summaries (my_port set,
    no stage filter), caching the expensive parse to disk per matchup.

    The cache lives inside the matchup dir (gitignored with pro_replays/*) and
    is keyed by file set + mtimes + my_char + schema version, so adding replays
    via /fetch-pro-replays auto-invalidates it. Cache I/O is best-effort: any
    error just falls back to a fresh parse.
    """
    cache_path = os.path.join(pro_dir, PRO_CACHE_FILENAME)
    sig = _pro_dir_signature(slp_files)

    try:
        with open(cache_path, "rb") as f:
            cached = pickle.load(f)
        if (cached.get("version") == PRO_CACHE_VERSION
                and cached.get("my_char", "").lower() == my_char.lower()
                and cached.get("signature") == sig):
            return cached["games"]
    except Exception:
        pass  # missing/stale/corrupt cache -> reparse

    games = []
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
        game_data["my_port"] = my_port
        games.append(game_data)

    try:
        with open(cache_path, "wb") as f:
            pickle.dump({"version": PRO_CACHE_VERSION, "my_char": my_char,
                         "signature": sig, "games": games}, f)
    except Exception:
        pass  # best-effort; a read-only dir just means no caching

    return games


def load_pro_stats(my_char, opp_char, stages=None):
    """Load and aggregate stats from pro replays for this matchup.

    Detects which port is my_char by character name. The parsed replays are
    cached per matchup (see _load_pro_games); the stage filter and aggregation
    run per call so different sets reuse the same cache.
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

    all_games = _load_pro_games(pro_dir, my_char, slp_files)
    # Pro datasets occasionally contain doubles replays; non-1v1 games map each
    # port's "opponent" to itself, fabricating punish/move attribution — drop them.
    game_summaries = [
        g for g in all_games
        if len(g.get("port_order", [])) == 2
        and (not stages or g.get("stage") in stages)
    ]

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

    out(f"{indent}SDs / game        : {stats.get('avg_sds', 0.0):.1f}")
    out(f"{indent}Avg shield time   : {stats['avg_shield_s']:.1f}s/game")
    out(f"{indent}Avg crouch time   : {stats['avg_crouch_s']:.1f}s/game")
    _def = stats['avg_shield_s'] + stats['avg_crouch_s']
    if _def > 0:
        _sh = 100.0 * stats['avg_shield_s'] / _def
        out(f"{indent}Shield vs crouch  : {_sh:.0f}% shield / {100 - _sh:.0f}% crouch  (of defensive time)")
    oo = stats.get("oos") or {}
    if oo.get("resolved"):
        avg_w = oo["total_wait"] / oo["resolved"]
        out(f"{indent}OOS response      : {oo['samples']} shield hits · avg {avg_w:.1f}f to act · "
            f"{_fmt_pct_dist(oo['categories'], top=5)}")
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
    # Ledge tech
    lt = stats.get("ledge_tech")
    if lt and lt.get("engagements", 0) > 0:
        eng = lt["engagements"]
        dwell_avg = lt["dwell_frames"] / lt["dwell_n"] if lt["dwell_n"] else 0.0
        inv = (f"{100.0 * lt['hang_invuln_frames'] / lt['hang_frames']:.0f}% invuln on ledge"
               if lt["hang_frames"] > 0 else "invuln n/a")
        out(f"{indent}Ledge tech        : {eng} grabs, avg {dwell_avg:.0f}f to act, {inv}")
        ld = lt["ledgedash_count"]
        if ld > 0:
            galint = lt["galint_sum"] / lt["galint_n"] if lt["galint_n"] else 0.0
            gpct   = 100.0 * lt["galint_pos"] / lt["galint_n"] if lt["galint_n"] else 0.0
            react  = lt["ld_reaction_sum"] / ld
            wland  = lt["ld_waveland_sum"] / ld
            fall   = (lt["ld_fall_sum"] / lt["ld_fall_n"]) if lt["ld_fall_n"] else 0.0
            dist   = lt["ld_distance_sum"] / lt["galint_n"] if lt["galint_n"] else 0.0
            out(f"{indent}  Ledgedash       : {ld} ledgedashes, GALINT avg {galint:.0f}f"
                f" best {lt['galint_max']}f ({gpct:.0f}% keep invuln)")
            out(f"{indent}                    reaction {react:.0f}f, fall {fall:.0f}f, "
                f"waveland {wland:.0f}f, dist {dist:.1f}")
        opt_str = ", ".join(
            f"{o} {lt['option_counts'][o]}"
            for o in LEDGE_OPTIONS if lt["option_counts"].get(o, 0) > 0
        )
        err = "  [!] ledge-jump = tech error" if lt["option_counts"].get("ledge_jump_direct", 0) > 0 else ""
        out(f"{indent}  Options         : {opt_str}{err}")
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
    # Move safety: per-move outcome / punished / spacing profile
    mu = stats.get("move_usage") or {}
    mu_shown = [(m, s) for m, s in sorted(mu.items(), key=lambda kv: -kv[1]["n"])
                if s["n"] >= 5][:6]
    if mu_shown:
        out(f"{indent}Move safety       : uses · hit/shield/whiff · punished wf,sh · dist hit→pun")
        for m, s in mu_shown:
            n = s["n"]
            hp, sp, wp = 100*s["hit"]/n, 100*s["shield"]/n, 100*s["whiff"]/n
            pwf = 100*s["punished_whiff"]/s["whiff"] if s["whiff"] else 0.0
            psh = 100*s["punished_shield"]/s["shield"] if s["shield"] else 0.0
            n_pun = s["punished_whiff"] + s["punished_shield"] + s["punished_hit"]
            dh = s["hit_dist_sum"]/s["hit"] if s["hit"] else 0.0
            dp = s["punished_dist_sum"]/n_pun if n_pun else 0.0
            dist_str = (f"{dh:4.1f}→{dp:.1f}" if n_pun and s["hit"] else
                        f"{dh:4.1f}→ —" if s["hit"] else "  — ")
            out(f"{indent}  {m:<11}     : {n:3d} · {hp:3.0f}/{sp:2.0f}/{wp:2.0f}% · "
                f"{pwf:3.0f}%,{psh:3.0f}% · {dist_str}")
    out(f"{indent}Avg punish dealt  : {stats['avg_punish']:.1f}%  ({len(stats['dealt_seqs'])} sequences)")
    if stats["dealt_seqs"]:
        total = len(stats["dealt_seqs"])
        out(f"{indent}Punish outcomes   : {stats['kills']} kills / {stats['edgeguards']} edgeguards / {stats['resets']} resets  ({100*stats['kills']//total}% kill rate)")
    out(f"{indent}Edgeguard (above) : {rate_str(stats['eg_above_conv'], stats['eg_above_att'])}")
    out(f"{indent}Edgeguard (below) : {rate_str(stats['eg_below_conv'], stats['eg_below_att'])}")
    eg_att_total = stats["eg_above_att"] + stats["eg_below_att"]
    if eg_att_total:
        free = stats.get("eg_free", 0)
        out(f"{indent}Free recoveries   : {free}/{eg_att_total} given "
            f"({100.0*free/eg_att_total:.0f}% uncontested)")
    if stats.get("eg_finishers"):
        fin = sorted(stats["eg_finishers"].items(), key=lambda kv: -kv[1])[:5]
        out(f"{indent}EG finishers      : " + " · ".join(f"{m} x{c}" for m, c in fin))
    lc = stats.get("ledge_coverage") or {}
    lc_shown = sorted((kv for kv in lc.items() if kv[1]["n"] > 0),
                      key=lambda kv: -kv[1]["n"])[:6]
    if lc_shown:
        out(f"{indent}Ledge coverage    : " + " · ".join(
            f"{opt} {s['punished']}/{s['n']}" for opt, s in lc_shown)
            + "  (their option, punished/total)")
    _write_gameplan_block(stats, out, indent)


def _fmt_pct_dist(d, top=4):
    """'{key} NN% · ...' for the top entries of a {key: count} dict."""
    tot = sum(d.values())
    if not tot:
        return "—"
    items = sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:top]
    return " · ".join(f"{k} {round(100*v/tot)}%" for k, v in items)


def _fmt_kill_pcts(d, top=4):
    """'avg NN% (move NN% xK · ...)' from a {move: {n, sum_pct}} dict."""
    tot_n = sum(v["n"] for v in d.values())
    if not tot_n:
        return "—"
    avg = sum(v["sum_pct"] for v in d.values()) / tot_n
    items = sorted(d.items(), key=lambda kv: -kv[1]["n"])[:top]
    moves = " · ".join(f"{m} {v['sum_pct']/v['n']:.0f}% x{v['n']}" for m, v in items)
    return f"avg {avg:.0f}% ({moves})"


MISTAKE_SHORT = {
    "caught_neutral": "caught", "grabbed_neutral": "grabbed",
    "landing_lag": "landing-lag", "whiffed": "whiffed",
    "attacked_into_shield": "OOS", "attacked_cc_grabbed": "CC'd",
    "missed_tech": "tech-chase", "reversal_victim": "reversed",
    "airdodged": "airdodge", "unknown": "?",
}


def _write_gameplan_block(stats, out, indent):
    """Neutral/punish-flow lines (matchup gameplan view)."""
    opened = stats.get("opened_by") or {}
    if opened:
        tot = sum(opened.values())
        items = sorted(opened.items(), key=lambda kv: kv[1], reverse=True)[:4]
        parts = []
        for k, v in items:
            move, _, mistake = k.partition("|")
            parts.append(f"{move}→{MISTAKE_SHORT.get(mistake, mistake)} {round(100*v/tot)}%")
        out(f"{indent}Got opened by     : {' · '.join(parts)}")
    src = stats.get("opening_sources") or {}
    if src:
        out(f"{indent}You open with     : {_fmt_pct_dist(src)}")
    so = stats.get("string_outcomes") or {}
    if so:
        flat = []
        for move, oc in so.items():
            for outcome, c in oc.items():
                if c:
                    flat.append((f"{move}→{outcome}", c))
        flat.sort(key=lambda kv: kv[1], reverse=True)
        out(f"{indent}Strings end       : " + " · ".join(f"{k} {c}" for k, c in flat[:6]))
    sbp = stats.get("string_by_pct") or {}
    if sbp:
        parts = []
        for label in PCT_BUCKET_ORDER:
            b = sbp.get(label)
            if not b or not b.get("n"):
                continue
            finished = b.get("kill", 0) + b.get("edgeguard", 0)
            parts.append(f"{label}: {round(100*finished/b['n'])}% kill/eg (n={b['n']})")
        if parts:
            out(f"{indent}Convert by %      : {' · '.join(parts)}")
    fu = stats.get("followups") or {}
    if fu:
        # Group by opener; show the punish tree for the top openers.
        by_opener = {}
        for key, dist in fu.items():
            opener, _, bucket = key.partition("|")
            by_opener.setdefault(opener, {})[bucket] = dist
        top_openers = sorted(
            by_opener.items(),
            key=lambda kv: -sum(sum(d.values()) for d in kv[1].values()))[:2]
        for opener, buckets in top_openers:
            if opener == "other":
                continue
            parts = []
            for label in PCT_BUCKET_ORDER:
                dist = buckets.get(label)
                if not dist:
                    continue
                tot = sum(dist.values())
                tops = sorted(dist.items(), key=lambda kv: -kv[1])[:3]
                inner = "/".join(f"{m} {round(100*c/tot)}%" for m, c in tops)
                parts.append(f"{label}: {inner}")
            if parts:
                out(f"{indent}After {opener:<12}: {' · '.join(parts)}")
    ykm = stats.get("your_kill_moves") or {}
    tkm = stats.get("their_kill_moves") or {}
    if ykm or tkm:
        out(f"{indent}Your kill moves   : {_fmt_pct_dist(ykm) if ykm else '—'}")
        out(f"{indent}Their kill moves  : {_fmt_pct_dist(tkm) if tkm else '—'}")
    ykp = stats.get("kill_pcts") or {}
    tkp = stats.get("their_kill_pcts") or {}
    if ykp or tkp:
        out(f"{indent}Kill percent      : you {_fmt_kill_pcts(ykp)}")
        out(f"{indent}Die at            : {_fmt_kill_pcts(tkp)}")
    dfor = stats.get("avg_punish")
    dag  = stats.get("avg_punish_against")
    if dfor is not None and dag is not None:
        out(f"{indent}Dmg per opening   : you {dfor:.1f}% / them {dag:.1f}%")
    rv = stats.get("reversals") or {}
    if rv.get("n"):
        n = rv["n"]
        kinds = rv.get("kinds") or {}
        line = (f"{n} (eg-try {kinds.get('edgeguard_try', 0)} / "
                f"combo-ext {kinds.get('combo_extension', 0)})  "
                f"cost {rv['dmg_sum']/n:.0f}%/ea")
        if rv.get("stocks"):
            line += f" + {rv['stocks']} stock(s)"
        if rv.get("moves"):
            line += f"  via {_fmt_pct_dist(rv['moves'], top=3)}"
        out(f"{indent}Reversed          : {line}")
    pm = stats.get("punished_moves") or {}
    if pm:
        out(f"{indent}You're punished on: {_fmt_pct_dist(pm)}")
    dg = stats.get("death_geo") or {}
    kg = stats.get("kill_geo") or {}
    if dg:
        out(f"{indent}You die           : {_fmt_pct_dist(dg)}")
    if kg:
        out(f"{indent}You kill (where)  : {_fmt_pct_dist(kg)}")
    ra, rd = stats.get("recovery_att", 0), stats.get("recovery_deaths", 0)
    if ra:
        out(f"{indent}Recovery back     : {round(100*(ra-rd)/ra)}%  ({ra-rd}/{ra} offstage trips)")
    cw, cl = stats.get("center_win"), stats.get("center_loss")
    if cw is not None and cl is not None:
        out(f"{indent}Center (W vs L)   : {cw:.0f}% in wins / {cl:.0f}% in losses")


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


def session_report(folder, my_code, count=None, sets=None, singles_only=False,
                   pool_matchups=False, json_path=None, files_override=None):
    if files_override:
        files = sorted((f for f in files_override if f.lower().endswith(".slp")),
                       key=os.path.getmtime)
        resolved = os.path.dirname(files[0]) if files else folder
    else:
        files, resolved = get_all_slp_files(folder, count)
    if not files:
        print(f"No .slp files found in: {resolved}")
        sys.exit(1)

    # When limiting by sets, scan newest-first so we can stop early,
    # then reverse to restore chronological order.
    scan_files = list(reversed(files)) if sets else files

    game_summaries = []
    skipped = []
    doubles_skipped = 0
    set_count = 0
    current_key = None  # track matchup changes (opp + characters) to count sets

    for path in scan_files:
        port = detect_port(path, my_code)
        if port is None:
            skipped.append(os.path.basename(path))
            continue

        _, game_data = analyze(path, focus_port=port)
        if game_data is None:
            skipped.append(os.path.basename(path))
            continue

        # Skip doubles / non-1v1 games when requested (4-player teams games)
        if singles_only and len(game_data.get("port_order", [])) > 2:
            doubles_skipped += 1
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

        # Count set transitions (newest-first when sets limit is active).
        # A set is one matchup, so split on opponent/character changes too.
        key = _matchup_key(game_data)
        if sets and key != current_key:
            if set_count >= sets:
                break
            set_count += 1
            current_key = key

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
    sets = group_into_sets(game_summaries, pool=pool_matchups)
    total_games = len(game_summaries)

    # Structured output for the long-term coach (coach.py). Always use
    # NON-pooled consecutive sets here, independent of the display --pool-matchups
    # setting, so each record is one sitting with a well-defined session_date
    # (a pooled set could otherwise span multiple days).
    if json_path:
        json_sets = group_into_sets(game_summaries, pool=False)
        with open(json_path, "w", encoding="utf-8") as jf:
            json.dump(build_json_payload(json_sets), jf, indent=2)
    session_stats = aggregate_stats(game_summaries)
    set_wins = sum(
        1 for s in sets
        if aggregate_stats(s)["wins"] > aggregate_stats(s)["losses"]
    )

    # Use the first game's OWN player port — the port number can differ between
    # games in a set, so the global scan port may index the opponent here.
    g0 = game_summaries[0]
    my_char = g0["ports"][g0["my_port"]]["char"]

    out(f"  Games  : {total_games}  |  Sets: {len(sets)}  |  Character: {my_char}")
    if skipped:
        out(f"  Skipped: {len(skipped)} games (no matching connect code)")
    if doubles_skipped:
        out(f"  Skipped: {doubles_skipped} doubles games (singles-only)")
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
    parser.add_argument("--code",  type=str, required=True, help="Your Slippi connect code (e.g. ABCD#123)")
    parser.add_argument("--sets",  type=int, default=None,  help="Number of most recent sets to include")
    parser.add_argument("--count", type=int, default=None,  help="Max number of recent games to include")
    parser.add_argument("--out",   type=str, default=None,  help="Write report to file")
    parser.add_argument("--singles-only", action="store_true",
                        help="Skip doubles (4-player) games")
    parser.add_argument("--pool-matchups", action="store_true",
                        help="Pool all games of a matchup into one set, even if "
                             "played in non-consecutive blocks")
    parser.add_argument("--json", type=str, default=None, dest="json_path",
                        help="Write structured per-set records to this JSON path "
                             "(for the long-term coach, coach.py)")
    parser.add_argument("--files", nargs="+", default=None,
                        help="Explicit .slp files to analyze (overrides folder scan "
                             "and --count); useful for re-running a specific session")
    args = parser.parse_args()

    report = session_report(args.folder, args.code, count=args.count, sets=args.sets,
                            singles_only=args.singles_only,
                            pool_matchups=args.pool_matchups,
                            json_path=args.json_path,
                            files_override=args.files)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Report saved to {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
