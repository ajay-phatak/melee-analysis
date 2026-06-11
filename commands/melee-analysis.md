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
   - `Melee Coach/Matchups/<my_char> vs <opp_char>.md` — per-matchup **neutral gameplan / flowchart** (NOT tech-skill — that lives in `Progress.md`). Built from the `trends.txt` MATCHUP GAMEPLANS block (running aggregate) + this session's `session.txt` lines:
     - **Neutral** — neutral score (your openings vs theirs); how you get opened (their starter → your mistake, with the biggest one as a flowchart fix); your opening sources; which of your own moves get punished (`You're punished on`); **move safety** (`Move safety` — per normal: uses, hit/shield/whiff split, punished-rate on whiffs and on shield, and avg startup distance when it hits vs when it gets punished — a spacing proxy with no hitbox data, so compare hit-vs-punished distances and rankings rather than absolutes; punished-on-shield = getting shield-grabbed, punished-on-hit = they CC'd it).
     - **Conversion** — how your strings end (flag heavy `uptilt→reset` = missed kill-confirm / SDI'd out; praise `uair/fair→kill`, `fair/bair→edgeguard`); **conversion by percent** (`Convert by %` — kill/edgeguard share per percent bucket; a low rate at 80%+ means you're still going for resets at kill percents); the **punish tree** (`After <opener>` — the next hit in the string per percent bucket: a true combo, tech-chase, or juggle catch within the string-continuity window, NOT necessarily a guaranteed follow-up; `end` = no continuation); your kill moves vs theirs; **kill percents** (`Kill percent` / `Die at` — avg percent stocks end at, per move; killing later than the pro baseline = missing confirms); the **reversal ledger** (`Reversed` — combo extensions / edgeguard tries that became THEIR opening, with damage/stock cost and the move that got reversed); damage per opening both ways.
     - **Stocks** — death/kill geography (edgehog/side/top/SD); recovery success; edgeguard (above/below); **free recoveries given** (`Free recoveries` — opponent recoveries you never contested; pros give very few) and **edgeguard finishers** (`EG finishers` — what actually ends your converted edgeguards; `edgehog` = died without a hit).
     - **Positioning** — center-stage control + win/loss correlation; **OOS response** (`OOS response` — what you do after a hit on shield and how fast: grab/usmash/jump/shielddrop are punishes, roll/drop are concessions — push the punish options); **ledge coverage** (`Ledge coverage` — the opponent's ledge-option mix and how often you punished each, `punished/total`; the most-used least-punished option is the flowchart gap).
     - Running record header + dated log row. No L-cancel/wavedash/ledgedash bullets here.
   - `Melee Coach/Progress.md` — dashboard (tech-skill focuses live here), regenerated from `trends.txt`: metric trajectory table (includes **SDs/game**, no stocks-lost), per-matchup records, and a terse **Current focuses** list.

6. **Coach in chat — present BOTH layers:** this session's headline stats + pro-baseline gaps, AND the long-term trends (what's improving / declining / stuck, per-matchup records). Give actionable focuses.

## Notes
- **Two reference frames:** pro baseline (`session.txt`) = "vs the ceiling"; trends (`trends.txt`) = "vs your past self."
- `history.json` is the source of truth (personal — keep it out of any shared repo). Obsidian notes are generated views, regenerable from history.
- Sets split by matchup `(opponent, my char, opp char)`.
- **SDs** (self-destructs, edgehog-aware) replace "stocks lost" as the tracked durability metric. Matchup notes use **gameplan** data: opener/ender moves (`down_special` = shine, `dthrow`, `uair`, …), conversion-by-percent + punish tree + kill percents (buckets 0-34 / 35-79 / 80-119 / 120+, by victim % at the opening), the reversal ledger, death/kill geography, recovery, and damage-per-opening — all in `session.txt` (per set) and `trends.txt` (per-matchup running aggregate).
- **Re-processing a past session** (after a pipeline change): `session_review.py … --files <those .slp>` (overrides `--count`) to scope it, then `coach.py ingest … --replace` to upsert the existing records with new fields.
- If a set shows `[no pro replays for X vs Y]`, `fetch_pro_replays.py` will add that matchup's baseline.
