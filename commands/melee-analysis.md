# Melee Session Analysis

Run a post-session Super Smash Bros. Melee analysis using Slippi replay data.

> **Install (Claude Code):** copy this file to `~/.claude/commands/melee-analysis.md`,
> then replace `ABCD#123` with your Slippi connect code and `path/to/Slippi` with your
> replay folder. Run the scripts from your local clone of this repo (or use absolute
> paths to them).

## Steps

1. Ask the user: "How many sets were in the session?" (if not already specified). Use `--sets N` to limit by set count.

2. Run the analysis script using the connect code for auto port detection:
```
python session_review.py "path/to/Slippi" --code ABCD#123 --sets <N> --pool-matchups --out session.txt
```
   - **Always include `--pool-matchups` by default.** Sets split by matchup (opponent + both characters), so when one opponent counterpicks back and forth (e.g. Falco → Fox → Falco), pooling merges all the Falco games into one set instead of two. It only ever pools games of the *same* matchup vs the *same* opponent, so it's safe as a default.
   - Add `--singles-only` if the player was in doubles games and wants them excluded.
   - Omit `--sets` if the user wants all recent games.
   - Use `--count <N>` instead if limiting by game count rather than set count.

3. Read and display the full contents of `session.txt`.

4. Offer analysis and discussion of the results, highlighting:
   - Any stats flagged with `[!]` or `[~]`.
   - Comparisons between the player and their opponent.
   - **Pro baseline comparison**: each set shows a "PRO BASELINE" stats block for the same matchup and stages, pulled from local replays. Point out where the player's numbers differ significantly from the pro level.
   - If a set shows `[no pro replays for X vs Y]`, mention that running `fetch_pro_replays.py` will download data for that matchup so future sessions include the comparison.
   - **Neutral analysis**: each set includes a "NEUTRAL ANALYSIS" block with:
     - *Neutral wins — you opened*: how neutral was opened (whiff punish, landing punish, aerial approach, dash attack, grounded attack, dash grab, walk-up grab, OOS grab, CC grab, airdodge punish, reversal).
     - *Neutral wins — opp opened, you did*: what the player was doing when neutral was lost (whiffed, airdodged, landing lag, attacked into shield, attacked and got CC'd, grabbed from neutral, caught in neutral, missed tech, got reversal'd).
     - *Punish continuations*: sequences that started from a tech situation rather than a fresh neutral exchange.
     - Point out imbalances (losing neutral far more than winning it), patterns in how neutral is being lost (e.g. consistently "grabbed from neutral" = susceptibility to grab, "landing lag" = risky aerial approaches), and whether win methods are varied or over-reliant on one tool.
   - **Ledge tech**: each set shows a "Ledge tech" block — ledge-option distribution plus ledgedash quality (GALINT average / best / % keeping invuln, and the reaction / fall-before-double-jump / waveland timing components). Point out low GALINT and which timing component is the leak.
   - Actionable things to work on.

## Notes on folder paths
- Run the scripts from your local clone of this repo, or use absolute paths to them.
- Regular online sessions: point at your Slippi folder (the script auto-selects the latest `YYYY-MM` subfolder).
- Tournament replays: point at the specific folder, e.g. `"path/to/Slippi/tournament-2026-04"`.
- Tournament files have player names in filenames instead of netplay codes — port detection uses filename parsing automatically.
