# Melee Analysis

Tooling for analyzing Super Smash Bros. Melee gameplay — fetches pro replay sets, parses Slippi `.slp` files, and produces game / set / session reviews.

**Status:** In progress — vibe-coded.

## What it does

- `fetch_pro_replays.py` — pulls pro replay sets from a HuggingFace dataset.
- `game_review.py` — single-game analysis from a Slippi `.slp` file.
- `set_review.py` — set-level (best-of-N) analysis, aggregating game reviews.
- `session_review.py` — multi-set / session-level review (sets split by matchup; pro-baseline comparison; `--json` emits structured per-set records).
- `coach.py` — long-term coach: persists each session's per-set records to a history file (`ingest`) and computes cross-session trends + per-matchup records (`trends`).
- `export_savestates.py` — lists the notable moments of a session (missed edgeguards, deaths, best punishes) and exports them as Training Mode savestates so you can *retry* them.

## Setup

```bash
pip install py-slippi huggingface_hub
```

(A proper `requirements.txt` will land as the project firms up.)

## Add your own replays

The `pro_replays/` folder is intentionally empty in this repo. To run anything you'll need replays there. Either:

- Run `python fetch_pro_replays.py` to download from the configured HuggingFace dataset, or
- Drop your own `.slp` files into `pro_replays/<player_or_event>/`.

See `pro_replays/README.md` for the expected directory layout.

## Run

```bash
python game_review.py pro_replays/<event>/<game>.slp
python set_review.py pro_replays/<event>/
python session_review.py pro_replays/<event>/
```

## Long-term coaching

Track progress across sessions, not just one-offs:

```bash
# 1. Analyze a session and emit structured per-set records
python session_review.py "path/to/Slippi" --code ABCD#123 --json session.json --out session.txt

# 2. Fold those records into a long-term history (idempotent — dedups by set)
python coach.py ingest session.json --history path/to/history.json

# 3. Compute cross-session trends + per-matchup records
python coach.py trends --history path/to/history.json
```

`history.json` is the source of truth (personal data — keep it out of shared repos). The
`/melee-analysis` command (below) wires these together and can also write per-session, per-matchup,
and dashboard notes to Obsidian.

## Redo the moment: Training Mode savestates

`session_review.py --json` also records the session's **notable moments** — every missed edgeguard,
death, and best punish, with the frame each one starts at. `export_savestates.py` turns those into
[Training Mode - Community Edition](https://github.com/UnclePunch/Training-Mode) savestates, so the
edgeguard you dropped becomes a practice rep with your opponent's real recovery replaying against you.

```bash
# What did I miss this session? (missed edgeguards by default)
python export_savestates.py list --json session.json

# Export them into Dolphin's Card A folder as .gci savestates
python export_savestates.py export --json session.json --pick 1,3,4
```

Requires [Training Mode - Community Edition](https://github.com/UnclePunch/Training-Mode) and a
Dolphin whose Slot A is set to **GCI Folder**; the exporter writes into that folder (auto-detected,
or pass `--out-dir`). The `.gci` construction is done by a small Rust sidecar in `savestate/`:

```bash
cd savestate && cargo build --release   # needs a Rust toolchain + network on first build
```

The binary isn't committed — build it once, or point `MELEE_SAVESTATE_EXE` at an existing copy.

**Credits.** This part stands entirely on other people's work:
[Fiction](https://github.com/Fiction52s)'s [Melee Improover](https://github.com/Fiction52s/Melee-Improover)
is where the idea of turning replay moments into practiceable savestates comes from;
[AlexanderHarrison](https://github.com/AlexanderHarrison)'s
[`tm_replay`](https://github.com/AlexanderHarrison/tm_replay) and
[`slp_parser`](https://github.com/AlexanderHarrison/slp_parser) do the actual savestate and
memory-card construction (the sidecar only picks the frames and the port);
[UnclePunch](https://github.com/UnclePunch) and Aitch's Training Mode - Community Edition loads them.

## Optional: install the analysis command (Claude Code)

`commands/melee-analysis.md` is a ready-made [Claude Code](https://claude.com/claude-code)
slash command that acts as a **long-term coach**: it runs the session review, folds the results into
your long-term history via `coach.py`, optionally writes per-session / per-matchup / dashboard notes
to Obsidian, and then discusses both this session and how you're trending.

Copy it into your commands folder and fill in your own details:

```bash
cp commands/melee-analysis.md ~/.claude/commands/melee-analysis.md
# then edit it: connect code (ABCD#123), Slippi folder, and your history.json path
```

Then run `/melee-analysis` in Claude Code after a session. The Obsidian step uses the
`obsidian-mcp-connector` MCP server and is optional — the history + trends work without it.
