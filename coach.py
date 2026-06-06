#!/usr/bin/env python3
"""
Melee Long-term Coach
=====================
Persists per-set analysis records across sessions and computes long-term trends,
so session_review.py output can be discussed in the context of how you're
trending over time (not just "this session").

Source of truth is a local history JSON (personal data — keep it out of any
shared repo). Obsidian notes are generated views written separately by the
analysis command.

Usage:
    # After a session, fold its structured output into history (idempotent):
    python coach.py ingest session.json --history history.json

    # Compute long-term trends from history:
    python coach.py trends --history history.json [--out trends.txt] [--json trends.json]

`session.json` is produced by:  session_review.py ... --json session.json
"""

import os
import sys
import io
import json
import hashlib
import argparse
import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Headline metrics tracked over time.
#   key:        field under record["metrics"]
#   (label, higher_is_better, epsilon, fmt)
# epsilon = how much polarity-adjusted change counts as a real move (vs "flat").
METRICS = {
    "lcancel_pct":         ("L-cancel %",          True,  2.0, "{:.0f}%"),
    "shield_s":            ("Shield time/game",    False, 0.5, "{:.1f}s"),
    "galint_keep_pct":     ("Ledgedash % keep inv", True, 3.0, "{:.0f}%"),
    "ledgedash_fall_avg":  ("Ledgedash fall→DJ",   False, 0.5, "{:.1f}f"),
    "neutral_win_pct":     ("Neutral win %",        True,  2.0, "{:.0f}%"),
    "kill_rate_pct":       ("Kill rate",            True,  1.0, "{:.0f}%"),
    "punish_pct":          ("Avg punish %",         True,  0.7, "{:.1f}%"),
    "edgeguard_below_pct": ("Edgeguard below %",    True,  3.0, "{:.0f}%"),
    "wavedash_pct":        ("Wavedash %",           True,  2.0, "{:.0f}%"),
    "f1_pct":              ("Frame-1 aerial %",     True,  2.0, "{:.0f}%"),
    "avg_stocks_lost":     ("Avg stocks lost",     False, 0.2, "{:.1f}"),
}
RECENT_SESSIONS = 3  # how many most-recent sessions count as "recent"


# ---------------------------------------------------------------------------
# History I/O
# ---------------------------------------------------------------------------
def set_key(files):
    """Stable identity for a set = hash of its sorted .slp filenames."""
    joined = "|".join(sorted(f for f in files if f))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def load_history(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("records", [])
            return data
        except Exception:
            print(f"WARN: could not read history at {path}; starting fresh.")
    return {"records": []}


def save_history(path, hist):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hist, f, indent=2)


def ingest(session_json, history_path):
    with open(session_json, encoding="utf-8") as f:
        payload = json.load(f)

    hist = load_history(history_path)
    existing = {r.get("set_key") for r in hist["records"]}

    added = 0
    for s in payload.get("sets", []):
        key = set_key(s.get("files", []))
        if key in existing:
            continue
        rec = dict(s)
        rec["set_key"] = key
        rec["ingested_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        hist["records"].append(rec)
        existing.add(key)
        added += 1

    hist["records"].sort(key=_record_sort_key)
    save_history(history_path, hist)
    print(f"Ingested {added} new set(s); history now has "
          f"{len(hist['records'])} record(s) at {history_path}")
    return added


def _record_sort_key(r):
    files = r.get("files") or [""]
    return (r.get("session_date", ""), files[0])


# ---------------------------------------------------------------------------
# Trend computation
# ---------------------------------------------------------------------------
def _weighted_avg(pairs):
    """pairs = list of (value, weight); skips None values. Returns float or None."""
    num = den = 0.0
    for v, w in pairs:
        if v is None:
            continue
        num += v * w
        den += w
    return (num / den) if den else None


def _session_points(records):
    """Collapse records into one games-weighted point per session_date, in order.
    Returns list of (date, {metric: value}, total_games)."""
    by_date = {}
    order = []
    for r in records:
        d = r.get("session_date", "")
        if d not in by_date:
            by_date[d] = []
            order.append(d)
        by_date[d].append(r)
    points = []
    for d in order:
        recs = by_date[d]
        games = sum(r.get("n_games", 0) for r in recs)
        vals = {}
        for m in METRICS:
            vals[m] = _weighted_avg(
                [(r["metrics"].get(m), r.get("n_games", 0)) for r in recs]
            )
        points.append((d, vals, games))
    return points


def _direction(recent, prior, higher_is_better, eps):
    if recent is None or prior is None:
        return "—"
    delta = (recent - prior) * (1 if higher_is_better else -1)
    if delta > eps:
        return "improving"
    if delta < -eps:
        return "declining"
    return "stable"


def _metric_trends(points):
    """Per-metric recent-vs-prior-vs-all-time trajectory for one set of session points."""
    n = len(points)
    if n >= 2:
        recent_k = min(RECENT_SESSIONS, n - 1)
    else:
        recent_k = n
    recent = points[-recent_k:] if recent_k else []
    prior = points[:-recent_k] if recent_k else []

    out = {}
    for m, (label, hib, eps, fmt) in METRICS.items():
        r_avg = _weighted_avg([(v[1].get(m), v[2]) for v in recent])
        p_avg = _weighted_avg([(v[1].get(m), v[2]) for v in prior]) if prior else None
        all_avg = _weighted_avg([(v[1].get(m), v[2]) for v in points])
        out[m] = {
            "label": label, "fmt": fmt, "higher_is_better": hib,
            "recent": r_avg, "prior": p_avg, "all_time": all_avg,
            "direction": _direction(r_avg, p_avg, hib, eps),
            "series": [(d, v[m]) for d, v, _ in points],
        }
    return out


def compute_trends(hist):
    records = sorted(hist.get("records", []), key=_record_sort_key)
    points = _session_points(records)
    n_sessions = len(points)

    # Trajectories computed PER CHARACTER so a secondary/troll pick (e.g. a single
    # Samus set) doesn't pollute the main character's trend. Trends are meaningful
    # at >=2 sessions for that character.
    by_char = {}
    for r in records:
        by_char.setdefault(r.get("my_char", "?"), []).append(r)
    char_trends = {}
    for ch, recs in by_char.items():
        pts = _session_points(recs)
        char_trends[ch] = {
            "n_sessions": len(pts),
            "games": sum(r.get("n_games", 0) for r in recs),
            "metric_trends": _metric_trends(pts),
        }

    # Per-matchup records + headline metric splits.
    matchups = {}
    for r in records:
        key = f"{r.get('my_char','?')} vs {r.get('opp_char','?')}"
        mk = matchups.setdefault(key, {"wins": 0, "losses": 0, "games": 0,
                                       "sessions": set(), "recs": []})
        mk["wins"] += r.get("wins", 0)
        mk["losses"] += r.get("losses", 0)
        mk["games"] += r.get("n_games", 0)
        mk["sessions"].add(r.get("session_date", ""))
        mk["recs"].append(r)
    matchup_summary = {}
    for key, mk in matchups.items():
        headline = {}
        for m in ("lcancel_pct", "shield_s", "galint_keep_pct",
                  "neutral_win_pct", "kill_rate_pct"):
            headline[m] = _weighted_avg(
                [(r["metrics"].get(m), r.get("n_games", 0)) for r in mk["recs"]]
            )
        matchup_summary[key] = {
            "wins": mk["wins"], "losses": mk["losses"], "games": mk["games"],
            "sessions": len(mk["sessions"]), "headline": headline,
        }

    return {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "n_sessions": n_sessions,
        "n_sets": len(records),
        "date_range": [points[0][0], points[-1][0]] if points else [None, None],
        "char_trends": char_trends,
        "matchups": matchup_summary,
    }


# ---------------------------------------------------------------------------
# Trend rendering (terse, scannable — matches the session report style)
# ---------------------------------------------------------------------------
def render_trends(tr):
    out = []
    a = out.append
    rng = tr["date_range"]
    a("=" * 70)
    a("  LONG-TERM TRENDS")
    a("=" * 70)
    a(f"  Sessions: {tr['n_sessions']}  |  Sets: {tr['n_sets']}  |  "
      f"Range: {rng[0]} -> {rng[1]}")
    a("")

    # Per-character trajectory tables (most-played first).
    chars = sorted(tr["char_trends"].items(),
                   key=lambda kv: kv[1]["games"], reverse=True)
    for ch, ct in chars:
        a("-" * 70)
        a(f"  {ch.upper()} — trajectory  ({ct['n_sessions']} sessions, {ct['games']}g)")
        if ct["n_sessions"] < 2:
            a("  (need >=2 sessions for a trend — accumulating)")
            a("")
            continue
        a("-" * 70)
        a(f"  {'Metric':<22}{'recent':>9}{'prior':>9}{'all-time':>10}   direction")
        for m, t in ct["metric_trends"].items():
            fmt = t["fmt"]
            def s(x, fmt=fmt):
                return fmt.format(x) if x is not None else "—"
            a(f"  {t['label']:<22}{s(t['recent']):>9}{s(t['prior']):>9}"
              f"{s(t['all_time']):>10}   {t['direction']}")
        a("")

    a("-" * 70)
    a("  PER-MATCHUP RECORD")
    a("-" * 70)
    for key, mk in sorted(tr["matchups"].items(),
                          key=lambda kv: kv[1]["games"], reverse=True):
        h = mk["headline"]
        def s(m, fmt):
            v = h.get(m)
            return fmt.format(v) if v is not None else "—"
        a(f"  {key:<22} {mk['wins']}-{mk['losses']}  "
          f"({mk['games']}g / {mk['sessions']} sess)   "
          f"Lcnc {s('lcancel_pct','{:.0f}%')}  "
          f"Shield {s('shield_s','{:.1f}s')}  "
          f"Neut {s('neutral_win_pct','{:.0f}%')}  "
          f"Kill {s('kill_rate_pct','{:.0f}%')}")
    a("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Melee long-term coach: persist analyses + trends.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ing = sub.add_parser("ingest", help="Fold a session's --json output into history (idempotent).")
    p_ing.add_argument("session_json", help="Path to session_review.py --json output")
    p_ing.add_argument("--history", required=True, help="Path to the history JSON store")

    p_tr = sub.add_parser("trends", help="Compute long-term trends from history.")
    p_tr.add_argument("--history", required=True, help="Path to the history JSON store")
    p_tr.add_argument("--out", default=None, help="Write the text trends report here")
    p_tr.add_argument("--json", dest="json_path", default=None,
                      help="Write structured trends JSON here")

    args = parser.parse_args()

    if args.cmd == "ingest":
        ingest(args.session_json, args.history)
    elif args.cmd == "trends":
        hist = load_history(args.history)
        tr = compute_trends(hist)
        report = render_trends(tr)
        if args.json_path:
            with open(args.json_path, "w", encoding="utf-8") as f:
                json.dump(tr, f, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(report)
            print(f"Trends written to {args.out}")
        else:
            print(report)


if __name__ == "__main__":
    main()
