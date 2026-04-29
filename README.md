# Melee Analysis

Tooling for analyzing Super Smash Bros. Melee gameplay — fetches pro replay sets, parses Slippi `.slp` files, and produces game / set / session reviews.

**Status:** In progress — vibe-coded.

## What it does

- `fetch_pro_replays.py` — pulls pro replay sets from a HuggingFace dataset.
- `game_review.py` — single-game analysis from a Slippi `.slp` file.
- `set_review.py` — set-level (best-of-N) analysis, aggregating game reviews.
- `session_review.py` — multi-set / session-level review.

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
