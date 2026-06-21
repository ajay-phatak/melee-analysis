# Melee Session Analysis — long-term coach

Analyze a Slippi session, **persist it to long-term history**, optionally **write notes to
Obsidian**, and coach on **both this session and how you're trending over time**.

**Coaching posture (important):** the analysis identifies *gaps*; **you decide the fixes.** This skill
facilitates your own notetaking — it surfaces the same gaps as before and offers a *tentative* fix as
a starting point to react to, but the prescription written into the notes is **your** call, in your
words. Never record an invented fix as if it were settled, and don't pad with generic advice. Gaps you
don't resolve are written as `fix: open` so they resurface next session.

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

5. **Surface gaps & collect your fixes — do this in chat BEFORE writing any notes.** Walk the same gaps the analysis found, grouped by matchup (neutral / conversion / stocks / positioning) plus the cross-cutting tech gaps. For each gap: state the gap with its numbers, offer **one tentative fix** marked as such (e.g. *"tentative — your call: …"*), then ask how you want to address it. The tentative is a prompt to react to, **not** a recommendation — keep it short, fast/aggressive (see coaching style), and never offer "play slower." Record your own wording as the fix. Anything you skip or leave undecided becomes `fix: open`. Don't overwhelm: batch the gaps so you can answer in one pass (e.g. a numbered list per matchup), and don't invent extra advice beyond the single tentative. **The fixes gathered here are what get written into the notes below.**

6. **(Optional) Update Obsidian** via the `obsidian-mcp-connector` MCP server (vault-relative paths under `Melee Coach/`; skip this whole step if you don't use Obsidian — history + trends still work). Create the folder/notes if missing; for existing notes, read first and update in place (don't duplicate). Match your terse, bulleted note style.
   - **Fixes come from you, not the model.** Throughout the blocks below, wherever the guidance says "the fix is X" / "prescribe Y" / "name the cover" / "flowchart fix", that names the *lever* to offer as a tentative in step 5 — the line written into the note is your chosen fix from step 5, or `fix: open` if unresolved. Write gap lines as `<gap> → fix: <your fix>` (or `→ fix: open`) so the notes stay scannable and open items are greppable. Keep the detected gap text exactly as before; only the prescription changes hands.
   - **`Melee Coach/Sessions/YYYY-MM-DD.md`** — one note per session. YAML frontmatter (`date`, `type: melee-session`, `matchups`, `record`); then a section per matchup-set with headline metrics, terse findings, and a "vs your trend" line. Overwrite if it already exists for that date.
   - **`Melee Coach/Matchups/<my_char> vs <opp_char>.md`** — one per matchup played. These are **neutral gameplans / flowcharts, NOT tech-skill reports** (tech lives in `Progress.md`). Use the per-matchup blocks from `trends.txt` (running aggregate) + this session's lines from `session.txt`. Structure:
     - **Neutral** — *Neutral score* (your openings vs theirs); *How you get opened* (their starter → your mistake; call out the biggest one as a flowchart gap, e.g. "Marth fairs your jump 45%"); *Your opening sources*; *Which of your own moves get punished* (`You're punished on` — name the riskiest button); *Move safety* (`Move safety` — per normal: uses, hit/shield/whiff split, punished-rate on whiff and on shield, avg startup distance hit vs punished. Spacing proxy, no hitbox data: compare hit-vs-punished distances and move rankings, not absolutes. Punished-on-shield = shield-grabbed → safer spacing/timing on shield, punished-on-hit = they CC'd it → respect CC percents. Coach the *spacing* of the button, never "press it less").
     - **Conversion** — *How your strings end* (flag heavy `uptilt→reset` = missed kill-confirm / they SDI'd out / escaped to platform; praise `uair/fair→kill` and `fair/bair→edgeguard`); *Conversion by percent* (`Convert by %` — kill/edgeguard share per percent bucket; low at 80%+ = still tech-chasing for resets at kill percents → the kill-confirm for that bucket is the lever); *Punish tree* (`After <opener>` — the next hit in the string per bucket: a true combo, tech-chase, or juggle catch within the string-continuity window, NOT necessarily a guaranteed follow-up — don't present these as combos; `end` = no continuation); *Your kill moves vs their kill moves*; *Kill percents* (`Kill percent` / `Die at` — avg percent stocks end at, per move; killing later than the pro baseline = missing confirms, dying earlier = DI/positioning leak); *Reversal ledger* (`Reversed` — combo extensions / edgeguard tries that became THEIR opening, with damage/stock cost and the move that got reversed; the lever is move choice on the extension, never "stop extending"); *Damage per opening, both ways*.
     - **Stocks** — *Death/kill geography* (edgehog / side / top / SD); *Recovery success*; *Edgeguard (above/below)*; *Free recoveries given* (`Free recoveries` — their recoveries you never contested; pros give almost none) and *Edgeguard finishers* (`EG finishers` — what ends your converted edgeguards; `edgehog` = no-hit kill. One dominant finisher = a coverage gap).
     - **Positioning** — center-stage control + win/loss correlation; *OOS response* (`OOS response` — response mix + avg frames-to-act after a hit on shield: grab/usmash/jump/shielddrop = punishes, roll/drop = concessions); *Ledge coverage* (`Ledge coverage`, shown `punished/total` per their option — their most-used least-punished ledge option is the flowchart gap, e.g. "Falco rolls 60%").
     - Keep a running record header. Append a dated log row; skip if that date+opponent row already exists. Do NOT put L-cancel / wavedash / ledgedash bullets here.
   - **`Melee Coach/Progress.md`** — the dashboard (this is where **tech-skill** focuses live). Regenerate each run from `trends.txt`: the metric trajectory table (includes **SDs/game**, no "stocks lost"), the per-matchup record table, and a terse **Current focuses** list built from the fixes you gave in step 5 plus any `fix: open` items — not invented from the metrics. Overwrite.

7. **Coach in chat — present BOTH layers:**
   - This session's headline stats + pro-baseline gaps (from `session.txt`).
   - Long-term trends (from `trends.txt`): call out what's **improving** vs **declining** vs **stuck**, and per-matchup records.
   - Echo back the fixes you committed to in step 5, and list any `fix: open` items still outstanding — don't substitute your own advice for them. **Coaching style (important):** when offering the step-5 tentatives or framing any gap, never frame the takeaway as "play slower / more patient / less movement." Lean toward fast-play tech (L-cancel, out-of-shield punishes, ledgedash fall-to-DJ timing), conversion, and edgeguard execution. Surface data honestly; keep tentatives aligned with fast, aggressive development — but the recorded fix is always yours.

## Notes
- **Two reference frames:** pro baseline (`session.txt`) = "vs the ceiling"; trends (`trends.txt`) = "vs your past self." Use both.
- `history.json` is the source of truth (personal — keep it out of any shared repo; e.g. store it inside a synced vault folder). Obsidian notes are generated views — regenerable from history.
- Sets split by matchup `(opponent, my char, opp char)`; `--pool-matchups` merges non-consecutive blocks of the same matchup vs the same opponent. `--singles-only` drops doubles.
- **SDs replaced "stocks lost"** as a tracked metric (stocks-lost just tracked win/loss). SD detection is edgehog-aware — an edgehog counts as the opponent's kill, not your SD.
- **Gameplan data**: `session.txt` shows per-set "Got opened by / You open with / Strings end / Convert by % / After <opener> / Kill moves / Kill percent / Die at / Reversed / You're punished on / You die / Recovery / Center"; `trends.txt` has a **MATCHUP GAMEPLANS** section with the running per-matchup aggregate. Move names use the global attack vocabulary (`down_special` = shine, `dthrow`, `uair`, etc.). Percent buckets are 0-34 / 35-79 / 80-119 / 120+ (victim % when the string started).
- Coaching style for matchups: the *tentative levers* you offer (step 5) lean **flowchart / DI / spacing / kill-confirm**, never "play slower" — but the recorded fix is always your own. The notes capture your prescriptions; `fix: open` marks gaps still awaiting one.
- **Re-processing a past session** (e.g. after a pipeline change): target its games with `session_review.py … --files <those .slp>` (overrides `--count`), then `coach.py ingest … --replace` to upsert the existing records with the new fields. Identify a date's games by the `YYYYMMDD` in the filename.
- If a set shows `[no pro replays for X vs Y]`, `fetch_pro_replays.py` will add that matchup's baseline.
