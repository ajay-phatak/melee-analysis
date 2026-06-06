# Melee Session Analysis — long-term coach

Analyze a Slippi session, **persist it to long-term history**, optionally **write notes to
Obsidian**, and coach on **both this session and how you're trending over time**.

> **Install (Claude Code):** copy this file to `~/.claude/commands/melee-analysis.md`, then fill in
> your details below — replace `ABCD#123` with your connect code, `path/to/Slippi` with your replay
> folder, and `path/to/history.json` with where you want the long-term store (e.g. inside an Obsidian
> vault folder so it syncs). The Obsidian step needs the `obsidian-mcp-connector` MCP server; skip
> that step if you don't use Obsidian — the history + trends still work.

## Steps

1. **Scope.** "Analyze my last set" → `--sets 1`. Whole session / unspecified → ask how many sets, or use `--count N`. Default to `--pool-matchups` and `--singles-only`.

2. **Analyze (text + structured):**
```
python session_review.py "path/to/Slippi" --code ABCD#123 --sets <N> --pool-matchups --singles-only --out session.txt --json session.json
```
   - `--pool-matchups` merges non-consecutive blocks of the same matchup vs the same opponent (safe default — only pools same matchup/opponent). `--singles-only` drops doubles.
   - First run for a matchup builds + caches its pro baseline (slow once, fast after).

3. **Persist + compute trends:**
```
python coach.py ingest session.json --history "path/to/history.json"
python coach.py trends --history "path/to/history.json" --out trends.txt
```
   - `ingest` is idempotent (dedups by set filenames); re-running a session is safe.

4. **Read** `session.txt` (this session + pro baselines) and `trends.txt` (long-term trends + per-matchup records).

5. **(Optional) Update Obsidian** via the `obsidian-mcp-connector` (vault-relative paths). Create the folder/notes if missing; update existing notes in place. Suggested layout under a `Melee Coach/` folder:
   - `Melee Coach/Sessions/YYYY-MM-DD.md` — one note per session: frontmatter (date, record, matchups) + a section per matchup-set with headline metrics, terse findings, and a "vs your trend" line.
   - `Melee Coach/Matchups/<my_char> vs <opp_char>.md` — per-matchup: running record, a short "what works / what to fix", and a dated log table.
   - `Melee Coach/Progress.md` — dashboard regenerated from `trends.txt`: metric trajectory table, per-matchup records, and a terse **Current focuses** list.

6. **Coach in chat — present BOTH layers:** this session's headline stats + pro-baseline gaps, AND the long-term trends (what's improving / declining / stuck, per-matchup records). Give actionable focuses.

## Notes
- **Two reference frames:** pro baseline (`session.txt`) = "vs the ceiling"; trends (`trends.txt`) = "vs your past self."
- `history.json` is the source of truth (personal — keep it out of any shared repo). Obsidian notes are generated views, regenerable from history.
- Sets split by matchup `(opponent, my char, opp char)`.
- If a set shows `[no pro replays for X vs Y]`, `fetch_pro_replays.py` will add that matchup's baseline.
