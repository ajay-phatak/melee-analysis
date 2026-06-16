#!/usr/bin/env python3
"""
Game Review
===========
Parses a single .slp file and outputs a structured analysis across 5 categories:
  1. Tech Skill       - L-cancel, wavedash, frame-1 aerials, aerial height split
  2. Stage Control    - % of time in center stage
  3. Edgeguarding     - above/below ledge conversion rates
  4. Neutral          - crouch vs shield time
  5. Punish           - avg damage, kill/edgeguard/reset outcomes

Usage:
    python game_review.py path/to/game.slp
    python game_review.py path/to/game.slp --port 1
    python game_review.py path/to/game.slp --code ABCD#123
    python game_review.py path/to/game.slp --out report.txt
"""

import sys
import io
import os
import re
import argparse
from collections import defaultdict, deque

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from slippi import Game
from slippi.id import ActionState, Stage
from slippi.event import Attack

FPS = 60

# ---------------------------------------------------------------------------
# Action state sets
# ---------------------------------------------------------------------------

DAMAGE_STATES = set(range(75, 100)) | {38}


def _sv(state):
    """Action-state as a raw int. py-slippi returns a plain int for action
    states it has no enum member for (e.g. some chars), and those lack
    ``.value`` — so normalize before comparing against int-based state sets."""
    return state.value if hasattr(state, "value") else state

AERIAL_LANDING_STATES = {
    ActionState.LANDING_AIR_N, ActionState.LANDING_AIR_F,
    ActionState.LANDING_AIR_B, ActionState.LANDING_AIR_HI, ActionState.LANDING_AIR_LW,
}
AERIAL_NAME_MAP = {
    ActionState.LANDING_AIR_N: "nair",
    ActionState.LANDING_AIR_F: "fair",
    ActionState.LANDING_AIR_B: "bair",
    ActionState.LANDING_AIR_HI: "uair",
    ActionState.LANDING_AIR_LW: "dair",
}
ATTACK_AIR_STATES = {
    ActionState.ATTACK_AIR_N, ActionState.ATTACK_AIR_F,
    ActionState.ATTACK_AIR_B, ActionState.ATTACK_AIR_HI, ActionState.ATTACK_AIR_LW,
}
ATTACK_AIR_NAME_MAP = {
    ActionState.ATTACK_AIR_N:  "nair",
    ActionState.ATTACK_AIR_F:  "fair",
    ActionState.ATTACK_AIR_B:  "bair",
    ActionState.ATTACK_AIR_HI: "uair",
    ActionState.ATTACK_AIR_LW: "dair",
}
AERIALS = ("nair", "fair", "bair", "uair", "dair")

# Map the global `Attack` enum (post.last_attack_landed) to short move labels.
# Character-agnostic, so DOWN_SPECIAL == shine for both spacies, etc.
ATTACK_NAME_MAP = {
    Attack.JAB_1: "jab", Attack.JAB_2: "jab", Attack.JAB_3: "jab",
    Attack.RAPID_JABS: "jab",
    Attack.DASH_ATTACK: "dash_attack",
    Attack.SIDE_TILT: "ftilt", Attack.UP_TILT: "utilt", Attack.DOWN_TILT: "dtilt",
    Attack.SIDE_SMASH: "fsmash", Attack.UP_SMASH: "usmash", Attack.DOWN_SMASH: "dsmash",
    Attack.NAIR: "nair", Attack.FAIR: "fair", Attack.BAIR: "bair",
    Attack.UAIR: "uair", Attack.DAIR: "dair",
    Attack.NEUTRAL_SPECIAL: "b_special", Attack.SIDE_SPECIAL: "side_special",
    Attack.UP_SPECIAL: "up_special", Attack.DOWN_SPECIAL: "down_special",
    Attack.FORWARD_THROW: "fthrow", Attack.BACK_THROW: "bthrow",
    Attack.UP_THROW: "uthrow", Attack.DOWN_THROW: "dthrow",
    Attack.PUMMEL: "pummel",
    Attack.GET_UP_ATTACK_FROM_BACK: "getup_attack",
    Attack.GET_UP_ATTACK_FROM_FRONT: "getup_attack",
    Attack.LEDGE_GET_UP_ATTACK_100: "ledge_attack",
    Attack.LEDGE_GET_UP_ATTACK: "ledge_attack",
}


def attack_name(att):
    """Short label for a post.last_attack_landed value (Attack enum or raw int)."""
    if att is None:
        return None
    try:
        return ATTACK_NAME_MAP.get(Attack(att), "other")
    except (ValueError, KeyError):
        return "other"


SHIELD_STATES = {
    ActionState.GUARD_ON, ActionState.GUARD, ActionState.GUARD_OFF,
    ActionState.GUARD_SET_OFF, ActionState.GUARD_REFLECT,
}
CLIFF_STATES = {
    ActionState.CLIFF_CATCH, ActionState.CLIFF_WAIT,
    ActionState.CLIFF_CLIMB_SLOW, ActionState.CLIFF_CLIMB_QUICK,
    ActionState.CLIFF_ATTACK_SLOW, ActionState.CLIFF_ATTACK_QUICK,
    ActionState.CLIFF_ESCAPE_SLOW, ActionState.CLIFF_ESCAPE_QUICK,
    ActionState.CLIFF_JUMP_SLOW_1, ActionState.CLIFF_JUMP_SLOW_2,
    ActionState.CLIFF_JUMP_QUICK_1, ActionState.CLIFF_JUMP_QUICK_2,
} | {s for s in (getattr(ActionState, n, None)
                 for n in ("CLIFF_WAIT_1", "CLIFF_WAIT_2")) if s is not None}
RECOVERY_STATES = {
    ActionState.FALL_SPECIAL, ActionState.FALL_SPECIAL_F,
    ActionState.FALL_SPECIAL_B, ActionState.ESCAPE_AIR,
}
DAMAGE_FLY_STATES = {
    ActionState.DAMAGE_FLY_HI, ActionState.DAMAGE_FLY_N, ActionState.DAMAGE_FLY_LW,
    ActionState.DAMAGE_FLY_TOP, ActionState.DAMAGE_FLY_ROLL,
}
# Victim is being held in a grab
CAPTURE_STATES = {
    ActionState.CAPTURE_PULLED_HI, ActionState.CAPTURE_WAIT_HI, ActionState.CAPTURE_DAMAGE_HI,
    ActionState.CAPTURE_PULLED_LW, ActionState.CAPTURE_WAIT_LW, ActionState.CAPTURE_DAMAGE_LW,
    ActionState.CAPTURE_CUT, ActionState.CAPTURE_JUMP, ActionState.CAPTURE_NECK, ActionState.CAPTURE_FOOT,
}
# Victim is being thrown
THROWN_STATES = {
    ActionState.THROWN_F, ActionState.THROWN_B, ActionState.THROWN_HI,
    ActionState.THROWN_LW, ActionState.THROWN_LW_WOMEN,
    ActionState.THROWN_F_F, ActionState.THROWN_F_B, ActionState.THROWN_F_HI, ActionState.THROWN_F_LW,
}
# The victim's THROWN state names the throw directly (more reliable than the
# attacker's last_attack_landed timing), so a grab opener reads as f/b/u/dthrow.
THROWN_NAME_MAP = {
    ActionState.THROWN_F: "fthrow", ActionState.THROWN_F_F: "fthrow",
    ActionState.THROWN_B: "bthrow", ActionState.THROWN_F_B: "bthrow",
    ActionState.THROWN_HI: "uthrow", ActionState.THROWN_F_HI: "uthrow",
    ActionState.THROWN_LW: "dthrow", ActionState.THROWN_LW_WOMEN: "dthrow",
    ActionState.THROWN_F_LW: "dthrow",
}
# Victim is knocked down (lying on the ground after a throw/knockdown)
DOWN_STATES = {
    ActionState.DOWN_BOUND_U, ActionState.DOWN_WAIT_U,
    ActionState.DOWN_BOUND_D, ActionState.DOWN_WAIT_D,
    ActionState.SHIELD_BREAK_DOWN_U, ActionState.SHIELD_BREAK_DOWN_D,
}
# Knockdown + tech-chase family: victim is on the ground OR teching / getting up
# (tech roll, getup roll, stand, getup-attack). During these the victim is still
# in a tech-chase situation (chaseable), not back to neutral — so a punish string
# stays alive across the knockdown until a regrab/hit follows or they truly escape.
KNOCKDOWN_TECH_STATES = DOWN_STATES | {
    s for s in (getattr(ActionState, n, None) for n in (
        "DOWN_DAMAGE_U", "DOWN_DAMAGE_D", "DOWN_STAND_U", "DOWN_STAND_D",
        "DOWN_FOWARD_U", "DOWN_FOWARD_D", "DOWN_BACK_U", "DOWN_BACK_D",
        "DOWN_SPOT_U", "DOWN_SPOT_D", "DOWN_ATTACK_U", "DOWN_ATTACK_D",
        "PASS", "PASSIVE", "PASSIVE_STAND_F", "PASSIVE_STAND_B",
        "PASSIVE_WALL", "PASSIVE_WALL_JUMP", "PASSIVE_CEIL",
    )) if s is not None
}
# Victim was in some form of hitstun / grab / knockdown (i.e. the opponent did
# something to them). Used to tell a real opponent kill from a self-destruct.
GOT_HIT_STATES = (set(DAMAGE_STATES) | DAMAGE_FLY_STATES
                  | CAPTURE_STATES | THROWN_STATES | DOWN_STATES)
# Death action-states. The state the victim is in when they die names the KO
# direction directly (stage-independent):
#   0    DeadDown                              -> off the bottom
#   1/2  DeadLeft / DeadRight                  -> off the side
#   3-10 DeadUp / DeadUpStar(Ice) / DeadUpFall*-> off the top (star / screen KO)
# Death is detected on ENTRY to one of these, NOT on the stock-count decrement:
# the stock only ticks down after the death animation finishes (a top star-KO
# lingers ~40-60 frames), by which point the victim's position no longer reflects
# the KO and the punish/recovery that caused it has already closed.
DEAD_STATES        = set(range(0, 11))
DEAD_SIDE_STATES   = {1, 2}
DEAD_BOTTOM_STATES = {0}
# All grounded and aerial attack states
ATTACK_STATES = frozenset({
    ActionState.ATTACK_11, ActionState.ATTACK_12, ActionState.ATTACK_13,
    ActionState.ATTACK_DASH,
    ActionState.ATTACK_S_3_HI, ActionState.ATTACK_S_3_HI_S, ActionState.ATTACK_S_3_S,
    ActionState.ATTACK_S_3_LW_S, ActionState.ATTACK_S_3_LW,
    ActionState.ATTACK_HI_3, ActionState.ATTACK_LW_3,
    ActionState.ATTACK_S_4_HI, ActionState.ATTACK_S_4_HI_S, ActionState.ATTACK_S_4_S,
    ActionState.ATTACK_S_4_LW_S, ActionState.ATTACK_S_4_LW,
    ActionState.ATTACK_HI_4, ActionState.ATTACK_LW_4,
    ActionState.ATTACK_AIR_N, ActionState.ATTACK_AIR_F, ActionState.ATTACK_AIR_B,
    ActionState.ATTACK_AIR_HI, ActionState.ATTACK_AIR_LW,
})
SQUAT_STATES = frozenset({ActionState.SQUAT, ActionState.SQUAT_WAIT, ActionState.SQUAT_RV})
GROUND_ATTACK_STATES = ATTACK_STATES - frozenset({
    ActionState.ATTACK_AIR_N, ActionState.ATTACK_AIR_F, ActionState.ATTACK_AIR_B,
    ActionState.ATTACK_AIR_HI, ActionState.ATTACK_AIR_LW,
}) - frozenset({ActionState.ATTACK_DASH})
DASH_STATES = frozenset({ActionState.DASH, ActionState.RUN, ActionState.RUN_DIRECT})
# Attack ActionState -> short move label, for naming the *victim's own* move
# when their attack created the opponent's opening (whiff-punished, shield-
# grabbed, CC-grabbed, reversal'd). Specials use char-specific state IDs and
# stay unnamed — consistent with the classifier, which only checks ATTACK_STATES.
ATTACK_STATE_NAME_MAP = {
    ActionState.ATTACK_11: "jab", ActionState.ATTACK_12: "jab", ActionState.ATTACK_13: "jab",
    ActionState.ATTACK_DASH: "dash_attack",
    ActionState.ATTACK_S_3_HI: "ftilt", ActionState.ATTACK_S_3_HI_S: "ftilt",
    ActionState.ATTACK_S_3_S: "ftilt",
    ActionState.ATTACK_S_3_LW_S: "ftilt", ActionState.ATTACK_S_3_LW: "ftilt",
    ActionState.ATTACK_HI_3: "utilt", ActionState.ATTACK_LW_3: "dtilt",
    ActionState.ATTACK_S_4_HI: "fsmash", ActionState.ATTACK_S_4_HI_S: "fsmash",
    ActionState.ATTACK_S_4_S: "fsmash",
    ActionState.ATTACK_S_4_LW_S: "fsmash", ActionState.ATTACK_S_4_LW: "fsmash",
    ActionState.ATTACK_HI_4: "usmash", ActionState.ATTACK_LW_4: "dsmash",
    ActionState.ATTACK_AIR_N: "nair", ActionState.ATTACK_AIR_F: "fair",
    ActionState.ATTACK_AIR_B: "bair", ActionState.ATTACK_AIR_HI: "uair",
    ActionState.ATTACK_AIR_LW: "dair",
}
LANDING_STATES = frozenset({
    ActionState.LANDING, ActionState.LANDING_FALL_SPECIAL,
    ActionState.LANDING_AIR_N, ActionState.LANDING_AIR_F,
    ActionState.LANDING_AIR_B, ActionState.LANDING_AIR_HI, ActionState.LANDING_AIR_LW,
})

# ---------------------------------------------------------------------------
# Post-landing tracker: state sets for categorizing the first action out of
# a normal landing. We use getattr(ActionState, ...) so the script tolerates
# slippi-lib versions that don't have every enum member.
# ---------------------------------------------------------------------------
def _optional_states(*names):
    return frozenset(s for s in (getattr(ActionState, n, None) for n in names) if s is not None)

POSTLAND_LANDING_STATES = AERIAL_LANDING_STATES | frozenset({ActionState.LANDING})

POSTLAND_SHIELD_STATES    = _optional_states("GUARD_ON", "GUARD")
POSTLAND_JAB_STATES       = _optional_states("ATTACK_11", "ATTACK_12", "ATTACK_13")
POSTLAND_TILT_STATES      = _optional_states(
    "ATTACK_S_3_HI", "ATTACK_S_3_HI_S", "ATTACK_S_3_S",
    "ATTACK_S_3_LW_S", "ATTACK_S_3_LW",
    "ATTACK_HI_3", "ATTACK_LW_3",
)
POSTLAND_SMASH_STATES     = _optional_states(
    "ATTACK_S_4_HI", "ATTACK_S_4_HI_S", "ATTACK_S_4_S",
    "ATTACK_S_4_LW_S", "ATTACK_S_4_LW",
    "ATTACK_HI_4", "ATTACK_LW_4",
)
POSTLAND_DASH_ATTACK      = _optional_states("ATTACK_DASH")
POSTLAND_GRAB_STATES      = _optional_states("CATCH", "CATCH_DASH")
POSTLAND_DASH_STATES      = _optional_states("DASH", "RUN", "RUN_DIRECT")
POSTLAND_WALK_STATES      = _optional_states("WALK_SLOW", "WALK_MIDDLE", "WALK_FAST")
POSTLAND_CROUCH_STATES    = _optional_states("SQUAT", "SQUAT_WAIT", "SQUAT_RV")
POSTLAND_SPOTDODGE_STATES = _optional_states("ESCAPE")
POSTLAND_ROLL_STATES      = _optional_states("ESCAPE_F", "ESCAPE_B")
POSTLAND_JUMP_STATES      = _optional_states("KNEE_BEND")

# (category, state-set) — first match wins during classification
POSTLAND_CATEGORY_SETS = (
    ("shield",      POSTLAND_SHIELD_STATES),
    ("jab",         POSTLAND_JAB_STATES),
    ("tilt",        POSTLAND_TILT_STATES),
    ("smash",       POSTLAND_SMASH_STATES),
    ("dash_attack", POSTLAND_DASH_ATTACK),
    ("grab",        POSTLAND_GRAB_STATES),
    ("dash",        POSTLAND_DASH_STATES),
    ("walk",        POSTLAND_WALK_STATES),
    ("crouch",      POSTLAND_CROUCH_STATES),
    ("spot_dodge",  POSTLAND_SPOTDODGE_STATES),
    ("roll",        POSTLAND_ROLL_STATES),
    ("jump",        POSTLAND_JUMP_STATES),
)
POSTLAND_CATEGORIES = tuple(c for c, _ in POSTLAND_CATEGORY_SETS) + ("other",)

WAIT_STATE = getattr(ActionState, "WAIT", None)
POSTLAND_AERIAL_BUCKETS = AERIALS + ("empty",)

# States that abort the post-landing watch (got hit, grabbed, knocked down)
POSTLAND_ABORT_STATES = (
    DAMAGE_FLY_STATES | CAPTURE_STATES | THROWN_STATES | DOWN_STATES
)

# ---------------------------------------------------------------------------
# Ledge-tech state sets (for LedgeTechTracker)
# ---------------------------------------------------------------------------
# Fresh ledge-grab intangibility budget (frames). Slippi replays don't store
# the live intangibility timer, so GALINT and ledge-stall invuln% are derived
# from this fixed budget: a fresh grab is intangible for this many frames,
# counting down every frame (on or off ledge) until the player is actionable.
# GALINT = max(0, LEDGE_INTANG_FRAMES - frames_from_grab_to_grounded_actionable).
#
# The budget is counted from the FIRST frame of CLIFF_CATCH. The catch animation
# itself eats some of these frames (~7 for most of the cast, but only ~3 for
# Link, giving him more actionable intangibility). Because frames_from_grab is
# measured from the real CLIFF_CATCH onset rather than assuming a fixed catch
# length, this 37-frame budget works correctly for every character incl. Link.
LEDGE_INTANG_FRAMES = 37
# Hanging on the ledge (intangible, no option committed yet)
LEDGE_HANG_STATES   = _optional_states("CLIFF_CATCH", "CLIFF_WAIT", "CLIFF_WAIT_1", "CLIFF_WAIT_2")
# Voluntary ledge-hang wait (excludes the forced CLIFF_CATCH animation)
LEDGE_WAIT_STATES   = _optional_states("CLIFF_WAIT", "CLIFF_WAIT_1", "CLIFF_WAIT_2")
# "Get-up from ledge" committing options (on-stage)
LEDGE_NEUTRAL_GETUP = _optional_states("CLIFF_CLIMB_SLOW", "CLIFF_CLIMB_QUICK")
LEDGE_ATTACK_GETUP  = _optional_states("CLIFF_ATTACK_SLOW", "CLIFF_ATTACK_QUICK")
LEDGE_ROLL_GETUP    = _optional_states("CLIFF_ESCAPE_SLOW", "CLIFF_ESCAPE_QUICK")
# Direct ledge jump (CLIFF_JUMP_*) — almost always a botched ledgedash, flagged as a tech error
LEDGE_JUMP_DIRECT   = _optional_states(
    "CLIFF_JUMP_SLOW_1", "CLIFF_JUMP_SLOW_2", "CLIFF_JUMP_QUICK_1", "CLIFF_JUMP_QUICK_2"
)
# Double jump (used mid-ledgedash and for dj-aerials)
DOUBLE_JUMP_STATES  = _optional_states("JUMP_AERIAL_F", "JUMP_AERIAL_B")
# Falling after releasing the ledge (start of a ledgedash / dj-aerial drop)
LEDGE_DROP_FALL_STATES = _optional_states("FALL", "FALL_AERIAL", "FALL_AERIAL_F", "FALL_AERIAL_B")
# Any committing get-up state (leaving the hang via a ground option)
LEDGE_GETUP_STATES = (
    LEDGE_NEUTRAL_GETUP | LEDGE_ATTACK_GETUP | LEDGE_ROLL_GETUP | LEDGE_JUMP_DIRECT
)
# Landing states that finish a waveland (ledgedash) onto the stage
LEDGE_LAND_STATES = _optional_states("LANDING", "LANDING_FALL_SPECIAL")
# Ledge-tech option categories (order = display order; first match wins)
LEDGE_OPTIONS = (
    "ledgedash", "slow_ledgedash", "dj_aerial", "ledge_refresh",
    "getup_attack", "neutral_getup", "roll_getup",
    "ledge_jump_direct", "other",
)

# A ledgedash whose voluntary CLIFF_WAIT exceeds this is treated as a
# "slow_ledgedash": the player sat on the ledge too long to be genuinely
# trying to retain GALINT, so it's excluded from the ledgedash/GALINT stats.
LEDGEDASH_MAX_CLIFFWAIT = 10

# ---------------------------------------------------------------------------
# Stage data
# center_x : inner edge of side platforms (or equivalent for FD)
# ledge_x  : approximate X of ledge grab point
# floor_y  : Y of main stage floor
# ---------------------------------------------------------------------------

DEFAULT_STAGE_DATA = {"center_x": 20.0, "ledge_x": 63.0, "floor_y": 0.0}

STAGE_DATA = {}
_stage_entries = [
    ("FINAL_DESTINATION",  {"center_x": 23.45, "ledge_x": 63.35, "floor_y": 0.0}),
    ("BATTLEFIELD",        {"center_x": 17.8,  "ledge_x": 68.4,  "floor_y": 0.0}),
    ("FOUNTAIN_OF_DREAMS", {"center_x": 14.0,  "ledge_x": 63.35, "floor_y": 0.0}),
    ("DREAM_LAND_N64",     {"center_x": 19.8,  "ledge_x": 77.27, "floor_y": 0.0}),
    ("YOSHIS_STORY",       {"center_x": 15.75, "ledge_x": 58.91, "floor_y": 0.0}),
    ("POKEMON_STADIUM",    {"center_x": 17.0,  "ledge_x": 87.75, "floor_y": 0.0}),
]
for _name, _data in _stage_entries:
    try:
        STAGE_DATA[getattr(Stage, _name)] = _data
    except AttributeError:
        pass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def frames_to_time(frame_idx):
    total_seconds = frame_idx / FPS
    minutes = int(total_seconds // 60)
    seconds = total_seconds % 60
    return f"{minutes}:{seconds:05.2f}"

def character_name(port_data):
    try:
        return port_data.character.name.replace("_", " ").title()
    except Exception:
        return "Unknown"

def port_label(port_idx, char_name):
    return f"P{port_idx + 1} ({char_name})"

def pct(num, den):
    return (100.0 * num / den) if den > 0 else 0.0


# ---------------------------------------------------------------------------
# Netplay / connect-code helpers
# ---------------------------------------------------------------------------

DIRECT_CODES_PATH = os.path.expandvars(
    r"%APPDATA%\Slippi Launcher\netplay\User\Slippi\direct-codes.json"
)

def get_direct_codes():
    """Return a set of normalized connect codes from Slippi's direct-codes.json."""
    import json
    try:
        with open(DIRECT_CODES_PATH, encoding="utf-8") as f:
            entries = json.load(f)
        result = set()
        for e in entries:
            code = e.get("connectCode", "")
            # Fullwidth unicode → ASCII
            normalized = "".join(
                chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else
                "#" if c == "＃" else c
                for c in code
            )
            result.add(normalized.upper())
        return result
    except Exception:
        return set()


def get_netplay_info(slp_path):
    """Return {port_idx: {"code": str, "name": str}} for players with netplay data."""
    try:
        g = Game(slp_path)
        result = {}
        for i, player in enumerate(g.metadata.players):
            if player and player.netplay and player.netplay.code:
                result[i] = {
                    "code": str(player.netplay.code),
                    "name": player.netplay.name or "",
                }
        return result
    except Exception:
        return {}

def detect_port(slp_path, my_code):
    """Return port index matching my_code (case-insensitive), or None.

    Primary: matches netplay connect code embedded in the file.
    Fallback: parses the filename for 'NAME (Char)' (tournament files have no
    netplay metadata) and matches the port playing that character.
    """
    # Primary: netplay code
    for port_idx, info in get_netplay_info(slp_path).items():
        if info["code"].upper() == my_code.upper():
            return port_idx

    # Fallback: filename-based detection for tournament files
    name = my_code.split("#")[0]  # "ABCD" from "ABCD#123"
    fname = os.path.basename(slp_path)
    m = re.search(r'\b' + re.escape(name) + r'\s*\(([^)]+)\)', fname, re.IGNORECASE)
    if m:
        char_name = m.group(1).strip().upper()  # e.g. "SHEIK", "FOX"
        try:
            g = Game(slp_path)
            for i, player in enumerate(g.start.players):
                if player and char_name in player.character.name.upper():
                    return i
        except Exception:
            pass

    return None

# ---------------------------------------------------------------------------
# Per-frame player snapshot
# ---------------------------------------------------------------------------

class PF:
    """Lightweight per-frame player snapshot."""
    __slots__ = ["state", "x", "y", "airborne", "stocks", "damage", "l_cancel",
                 "jumps", "last_attack", "last_hit_by"]

    def __init__(self, pre, post):
        self.state    = post.state
        self.x        = post.position.x
        self.y        = post.position.y
        self.airborne = post.airborne
        self.stocks   = post.stocks
        self.damage   = post.damage
        self.l_cancel = post.l_cancel  # 1=success, 2=miss, None=not landing
        self.jumps    = post.jumps     # jumps remaining
        # Move this player last *landed* on someone (global Attack enum / int).
        self.last_attack = post.last_attack_landed
        # Port that last hit this player; None / sentinel when never hit.
        self.last_hit_by = post.last_hit_by


# ---------------------------------------------------------------------------
# 1. Tech Skill Tracker
# ---------------------------------------------------------------------------

class TechSkillTracker:
    def __init__(self):
        # L-cancel
        self.l_cancel_attempts = 0
        self.l_cancel_success  = 0
        # Aerial height split
        self.high_aerials = 0   # autocancelled (ATTACK_AIR -> LANDING)
        self.low_aerials  = 0   # needed L-cancel (-> LANDING_AIR_*)
        # Wavedash (KNEE_BEND -> frame-1 airborne -> ESCAPE_AIR -> land)
        self.wd_attempts = 0
        self.wd_perfect  = 0    # ESCAPE_AIR on frame 1 of airborne
        # Frame-1 aerials (ATTACK_AIR within 5 frames of jump)
        self.f1_attempts = 0    # aerials within 5 frames of jump
        self.f1_perfect  = 0    # aerial on exactly frame 1 of airborne
        # Per-aerial breakdown (nair/fair/bair/uair/dair)
        self.lc_att_by_aerial = {a: 0 for a in AERIALS}
        self.lc_suc_by_aerial = {a: 0 for a in AERIALS}
        self.high_by_aerial   = {a: 0 for a in AERIALS}
        self.low_by_aerial    = {a: 0 for a in AERIALS}
        self.f1_att_by_aerial = {a: 0 for a in AERIALS}
        self.f1_prf_by_aerial = {a: 0 for a in AERIALS}

        self._prev_state      = None
        self._prev_airborne   = None
        self._frames_airborne = 0    # frames since jump (0 = not tracking)
        self._from_jump       = False
        self._airdodge_frame  = None  # which airborne frame ESCAPE_AIR started

    def feed(self, curr):
        state   = curr.state
        prev    = self._prev_state
        air     = curr.airborne
        prev_air = self._prev_airborne

        # --- L-cancel (native field, only set on first landing frame) ---
        if state in AERIAL_LANDING_STATES and (prev not in AERIAL_LANDING_STATES):
            self.low_aerials += 1
            aerial = AERIAL_NAME_MAP[state]
            self.low_by_aerial[aerial] += 1
            if curr.l_cancel == 1:
                self.l_cancel_attempts += 1
                self.l_cancel_success  += 1
                self.lc_att_by_aerial[aerial] += 1
                self.lc_suc_by_aerial[aerial] += 1
            elif curr.l_cancel == 2:
                self.l_cancel_attempts += 1
                self.lc_att_by_aerial[aerial] += 1

        # --- High aerials (autocancel) ---
        if prev in ATTACK_AIR_STATES and state == ActionState.LANDING:
            self.high_aerials += 1
            self.high_by_aerial[ATTACK_AIR_NAME_MAP[prev]] += 1

        # --- Jump tracking: KNEE_BEND ends and player becomes airborne ---
        if prev == ActionState.KNEE_BEND and state != ActionState.KNEE_BEND and air:
            self._frames_airborne = 1
            self._from_jump       = True
            self._airdodge_frame  = None
        elif self._from_jump:
            if air:
                self._frames_airborne += 1
                if self._frames_airborne > 10:
                    self._from_jump = False  # too long, no longer tracking
            else:
                self._from_jump = False

        # --- Wavedash: ESCAPE_AIR during jump window -> lands ---
        if self._from_jump and state == ActionState.ESCAPE_AIR and prev != ActionState.ESCAPE_AIR:
            self._airdodge_frame = self._frames_airborne

        if self._airdodge_frame is not None:
            if prev == ActionState.ESCAPE_AIR and state in (ActionState.LANDING, ActionState.LANDING_FALL_SPECIAL):
                self.wd_attempts += 1
                if self._airdodge_frame <= 2:  # frame 2 = input on first airborne frame (1-frame state lag)
                    self.wd_perfect += 1
                self._airdodge_frame = None
                self._from_jump = False
            elif state not in (ActionState.ESCAPE_AIR, ActionState.LANDING, ActionState.LANDING_FALL_SPECIAL):
                self._airdodge_frame = None  # real airdodge, not wavedash

        # --- Frame-1 aerials: ATTACK_AIR within 5 frames of jump ---
        if self._from_jump and state in ATTACK_AIR_STATES and prev not in ATTACK_AIR_STATES:
            self.f1_attempts += 1
            aerial = ATTACK_AIR_NAME_MAP[state]
            self.f1_att_by_aerial[aerial] += 1
            if self._frames_airborne <= 2:  # frame 2 = input on first airborne frame (1-frame state lag)
                self.f1_perfect += 1
                self.f1_prf_by_aerial[aerial] += 1

        self._prev_state    = state
        self._prev_airborne = air


# ---------------------------------------------------------------------------
# 1a. Ledge Tech Tracker
# Classifies what a player does off every ledge grab and measures ledgedash
# quality via GALINT (grounded actionable ledge intangibility).
#
# A "ledge engagement" begins when the player enters a hang state (CLIFF_CATCH/
# CLIFF_WAIT) and ends when they commit to an option:
#   ledgedash, dj_aerial, ledge_refresh (regrab), getup_attack, neutral_getup,
#   roll_getup, ledge_jump_direct (botched-ledgedash tech error), other.
# For ledgedashes we also record the 3 timing components (release reaction,
# fall frames before the double jump, waveland landing-lag) and the inward
# distance travelled, so the GALINT-vs-distance tradeoff is visible.
# ---------------------------------------------------------------------------

class LedgeTechTracker:
    DROP_TIMEOUT = 60   # max frames after ledge release to resolve a drop option

    def __init__(self, ledge_x):
        self.ledge_x            = ledge_x
        self.engagements        = 0
        self.option_counts      = {o: 0 for o in LEDGE_OPTIONS}
        self.hang_frames        = 0
        self.hang_invuln_frames = 0
        self.dwell_frames       = 0      # summed over resolved engagements
        self.dwell_n            = 0
        self.ledgedash_count    = 0
        self.galint_sum         = 0
        self.galint_n           = 0
        self.galint_max         = 0      # best (highest) GALINT seen
        self.galint_pos         = 0      # ledgedashes that retained any invuln (GALINT > 0)
        self.ld_reaction_sum    = 0      # release dwell (catch -> release) per ledgedash
        self.ld_fall_sum        = 0      # release -> double jump
        self.ld_fall_n          = 0
        self.ld_waveland_sum    = 0      # land -> grounded-actionable
        self.ld_distance_sum    = 0.0    # inward distance from ledge at actionable
        self.events             = []     # (frame, option) per resolved engagement;
                                         # also (frame, "hit_on_ledge") on aborts
        self._mode  = "IDLE"
        self._prev  = None
        self._frame = 0
        self._reset_engagement()

    def _reset_engagement(self):
        self._hang           = 0
        self._cliffwait      = 0   # voluntary wait frames (excludes CLIFF_CATCH)
        self._since_grab     = 0   # frames since the ledge grab (drives GALINT model)
        self._release_dwell  = 0
        self._drop_elapsed   = 0
        self._dj_done        = False
        self._fall_before_dj = 0
        self._airdodged      = False
        self._waveland_lag   = 0
        self._ld_distance    = 0.0

    def _commit_hang(self):
        """Leaving the hang: bank hang frames + modeled invuln, freeze dwell."""
        self.hang_frames += self._hang
        # Ledge intangibility is a fixed budget spent from the grab, so the first
        # LEDGE_INTANG_FRAMES of the hang are intangible (deterministic model).
        self.hang_invuln_frames += min(self._hang, LEDGE_INTANG_FRAMES)
        self._release_dwell = self._hang

    def _resolve(self, opt):
        """Finish a (non-ledgedash) engagement and return to IDLE."""
        self.option_counts[opt] += 1
        self.events.append((self._frame, opt))
        self.dwell_frames += self._release_dwell
        self.dwell_n      += 1
        self._mode = "IDLE"
        self._reset_engagement()

    def _maybe_dj(self, curr):
        if self._dj_done:
            return
        consumed = (
            self._prev is not None
            and curr.jumps is not None and self._prev.jumps is not None
            and curr.jumps < self._prev.jumps
        )
        if curr.state in DOUBLE_JUMP_STATES or consumed:
            self._dj_done = True
            self._fall_before_dj = self._drop_elapsed

    def _maybe_drop_resolve(self, curr):
        if self._mode != "DROP":
            return
        state = curr.state
        if state == ActionState.ESCAPE_AIR:
            self._airdodged = True
            return
        if self._airdodged and state in LEDGE_LAND_STATES:
            # airdodge -> land on stage = waveland -> ledgedash; score GALINT next
            self._mode = "WAVELAND"
            self._waveland_lag = 0
            return
        if self._dj_done and state in ATTACK_AIR_STATES:
            self._resolve("dj_aerial")
            return
        if (not self._airdodged) and state in LEDGE_LAND_STATES:
            # fell and landed without a waveland (not a ledgedash)
            self._resolve("other")
            return

    def _finish_ledgedash(self, curr):
        # Sat on the ledge too long to be a genuine GALINT attempt -> not a ledgedash
        if self._cliffwait > LEDGEDASH_MAX_CLIFFWAIT:
            self._resolve("slow_ledgedash")
            return
        # GALINT = ledge intangibility budget minus frames elapsed grab -> actionable
        galint = max(0, LEDGE_INTANG_FRAMES - self._since_grab)
        self._ld_distance = max(0.0, self.ledge_x - abs(curr.x))
        self.ledgedash_count += 1
        self.option_counts["ledgedash"] += 1
        self.events.append((self._frame, "ledgedash"))
        self.ld_reaction_sum += self._release_dwell
        self.ld_waveland_sum += self._waveland_lag
        if self._dj_done:
            self.ld_fall_sum += self._fall_before_dj
            self.ld_fall_n   += 1
        self.galint_sum      += galint
        self.galint_n        += 1
        if galint > self.galint_max:
            self.galint_max = galint
        if galint > 0:
            self.galint_pos += 1
        self.ld_distance_sum += self._ld_distance
        self.dwell_frames += self._release_dwell
        self.dwell_n      += 1
        self._mode = "IDLE"
        self._reset_engagement()

    def feed(self, frame_idx, curr):
        state = curr.state
        self._frame = frame_idx

        # Got hit / grabbed / knocked down / dead mid-engagement -> abandon it.
        # Getting hit out of a live engagement is the opponent covering the
        # ledge — record it as its own event for coverage analysis.
        if state in POSTLAND_ABORT_STATES:
            if self._mode != "IDLE" and state in GOT_HIT_STATES:
                self.events.append((frame_idx, "hit_on_ledge"))
            self._mode = "IDLE"
            self._reset_engagement()
            self._prev = curr
            return

        # Frames since the ledge grab accumulate across all active phases and
        # drive the deterministic GALINT / ledge-invuln model.
        if self._mode in ("HANG", "DROP", "WAVELAND"):
            self._since_grab += 1

        if self._mode == "IDLE":
            if state in LEDGE_HANG_STATES:
                self.engagements += 1
                self._reset_engagement()
                self._mode = "HANG"
                self._hang = 1
                if state in LEDGE_WAIT_STATES:
                    self._cliffwait = 1

        elif self._mode == "HANG":
            if state in LEDGE_HANG_STATES:
                self._hang += 1
                if state in LEDGE_WAIT_STATES:
                    self._cliffwait += 1
            elif state in LEDGE_GETUP_STATES:
                self._commit_hang()
                if state in LEDGE_ATTACK_GETUP:
                    self._resolve("getup_attack")
                elif state in LEDGE_NEUTRAL_GETUP:
                    self._resolve("neutral_getup")
                elif state in LEDGE_ROLL_GETUP:
                    self._resolve("roll_getup")
                else:
                    self._resolve("ledge_jump_direct")
            else:
                # released the ledge by dropping off
                self._commit_hang()
                self._mode = "DROP"
                self._drop_elapsed = 0
                self._maybe_dj(curr)
                self._maybe_drop_resolve(curr)

        elif self._mode == "DROP":
            self._drop_elapsed += 1
            if state in LEDGE_HANG_STATES:
                self._resolve("ledge_refresh")
            else:
                self._maybe_dj(curr)
                self._maybe_drop_resolve(curr)
                if self._mode == "DROP" and self._drop_elapsed > self.DROP_TIMEOUT:
                    self._resolve("other")

        elif self._mode == "WAVELAND":
            self._waveland_lag += 1
            if state not in LEDGE_LAND_STATES and not curr.airborne:
                # first grounded-actionable frame out of the waveland
                self._finish_ledgedash(curr)
            elif curr.airborne or self._waveland_lag > self.DROP_TIMEOUT:
                # took back off / abnormal -> still score it as a ledgedash
                self._finish_ledgedash(curr)

        self._prev = curr

    def summary(self):
        return {
            "engagements":        self.engagements,
            "option_counts":      dict(self.option_counts),
            "hang_frames":        self.hang_frames,
            "hang_invuln_frames": self.hang_invuln_frames,
            "dwell_frames":       self.dwell_frames,
            "dwell_n":            self.dwell_n,
            "ledgedash_count":    self.ledgedash_count,
            "galint_sum":         self.galint_sum,
            "galint_n":           self.galint_n,
            "galint_max":         self.galint_max,
            "galint_pos":         self.galint_pos,
            "ld_reaction_sum":    self.ld_reaction_sum,
            "ld_fall_sum":        self.ld_fall_sum,
            "ld_fall_n":          self.ld_fall_n,
            "ld_waveland_sum":    self.ld_waveland_sum,
            "ld_distance_sum":    self.ld_distance_sum,
        }


# ---------------------------------------------------------------------------
# 1b. Post-Landing Tracker
# Tracks the first action a player takes out of every "normal landing"
# (LANDING or LANDING_AIR_*), how long they spent in WAIT before acting,
# and which aerial (if any) preceded the landing.
# ---------------------------------------------------------------------------

class PostLandingTracker:
    TIMEOUT_FRAMES = 60

    def __init__(self):
        self.samples           = 0
        self.total_wait_frames = 0
        self.cat_counts        = {c: 0 for c in POSTLAND_CATEGORIES}
        self.by_aerial = {
            a: {
                "samples": 0,
                "total_wait_frames": 0,
                "categories": {c: 0 for c in POSTLAND_CATEGORIES},
            } for a in POSTLAND_AERIAL_BUCKETS
        }
        self._mode         = "IDLE"   # "IDLE" | "ARMED" | "COUNTING"
        self._aerial       = None
        self._wait_frames  = 0
        self._elapsed      = 0
        self._prev_state   = None

    def feed(self, curr):
        state = curr.state
        prev  = self._prev_state

        # If we're armed, run abort/timeout checks first
        if self._mode != "IDLE":
            self._elapsed += 1
            if self._elapsed > self.TIMEOUT_FRAMES:
                self._reset()
                self._prev_state = state
                return
            if state in POSTLAND_ABORT_STATES or curr.stocks == 0:
                self._reset()
                self._prev_state = state
                return

        # Detect entry into a tracked landing (LANDING or LANDING_AIR_*)
        if state in POSTLAND_LANDING_STATES and (prev not in POSTLAND_LANDING_STATES):
            if state in AERIAL_LANDING_STATES:
                self._aerial = AERIAL_NAME_MAP[state]
            elif prev in ATTACK_AIR_STATES:
                self._aerial = ATTACK_AIR_NAME_MAP[prev]
            else:
                self._aerial = "empty"
            self._mode        = "ARMED"
            self._wait_frames = 0
            self._elapsed     = 0
            self._prev_state  = state
            return

        # Inside a post-landing watch
        if self._mode in ("ARMED", "COUNTING"):
            # Still in landing animation (e.g. multi-frame LANDING_AIR_N) — keep waiting
            if state in POSTLAND_LANDING_STATES:
                self._prev_state = state
                return
            # WAIT: count and stay armed
            if WAIT_STATE is not None and state == WAIT_STATE:
                self._mode = "COUNTING"
                self._wait_frames += 1
                self._prev_state = state
                return
            # First non-WAIT actionable state — classify and record
            cat = self._classify(state)
            self._record(cat)
            self._reset()
            self._prev_state = state
            return

        self._prev_state = state

    def _classify(self, state):
        for cat, sset in POSTLAND_CATEGORY_SETS:
            if state in sset:
                return cat
        return "other"

    def _record(self, cat):
        wf = self._wait_frames
        self.samples += 1
        self.total_wait_frames += wf
        self.cat_counts[cat] += 1
        bucket = self.by_aerial[self._aerial]
        bucket["samples"] += 1
        bucket["total_wait_frames"] += wf
        bucket["categories"][cat] += 1

    def _reset(self):
        self._mode        = "IDLE"
        self._aerial      = None
        self._wait_frames = 0
        self._elapsed     = 0

    def summary(self):
        avg = self.total_wait_frames / self.samples if self.samples else 0.0
        return {
            "samples":           self.samples,
            "total_wait_frames": self.total_wait_frames,
            "avg_frames_to_act": avg,
            "categories":        dict(self.cat_counts),
            "by_aerial": {
                a: {
                    "samples":           b["samples"],
                    "total_wait_frames": b["total_wait_frames"],
                    "categories":        dict(b["categories"]),
                } for a, b in self.by_aerial.items()
            },
        }


# ---------------------------------------------------------------------------
# 2. Stage Control Tracker
# ---------------------------------------------------------------------------

class StageControlTracker:
    def __init__(self, center_x):
        self.center_x      = center_x
        self.center_frames = 0
        self.total_frames  = 0

    def feed(self, curr):
        if curr.stocks == 0:
            return
        self.total_frames += 1
        if abs(curr.x) <= self.center_x:
            self.center_frames += 1

    def center_pct(self):
        return pct(self.center_frames, self.total_frames)


# ---------------------------------------------------------------------------
# 3. Edgeguard Tracker  (tracks the player being edgeguarded)
# ---------------------------------------------------------------------------

class EdgeguardTracker:
    """
    Tracks recovery situations for one player.
    A situation starts when they go offstage (or grab ledge).
    Ends when they: (a) return to stage and are actionable, or (b) die.
    """
    def __init__(self, ledge_x, floor_y):
        self.ledge_x    = ledge_x
        self.floor_y    = floor_y
        self.situations = []

        self._active       = False
        self._start        = 0
        self._prev_jumps   = None   # jumps remaining on previous offstage frame
        self._dj_used      = False  # double jump used while offstage
        self._recovery_y   = None   # Y when recovery action (airdodge/helpless) initiated
        self._prev_state   = None
        self._challenged   = False  # edgeguarder did something (attack near edge,
                                    # ledge hog, or actually hit the recoverer)
        self._last_hit_move = None  # last move that connected during the recovery

    def _is_offstage(self, pf):
        if pf.state in CLIFF_STATES:
            return True
        return abs(pf.x) > self.ledge_x and pf.airborne

    def _is_safe_on_stage(self, pf):
        return (
            not pf.airborne
            and abs(pf.x) <= self.ledge_x + 5
            and pf.state not in CLIFF_STATES
            and _sv(pf.state) not in DAMAGE_STATES
            and pf.stocks > 0
        )

    def feed(self, frame_idx, curr, prev, opp=None):
        offstage      = self._is_offstage(curr)
        prev_offstage = self._is_offstage(prev) if prev else False

        if not self._active:
            if offstage and not prev_offstage:
                self._active     = True
                self._start      = frame_idx
                self._prev_jumps = curr.jumps
                self._dj_used    = False
                self._recovery_y = None
                self._prev_state = curr.state
                self._challenged    = False
                self._last_hit_move = None
                # Already in knockback when crossing the ledge line: the
                # launching move is the prospective finisher, so a clean
                # bair KO out the side reads "bair", not "edgehog".
                if (opp is not None
                        and (_sv(curr.state) in DAMAGE_STATES
                             or curr.state in DAMAGE_FLY_STATES)):
                    self._last_hit_move = attack_name(opp.last_attack)
        else:
            # Did the edgeguarder contest this recovery? Attacking near the
            # edge, holding the ledge (hog), or landing an actual hit all
            # count; an attack thrown mid-stage does not.
            if opp is not None:
                near_edge = abs(opp.x) > self.ledge_x - 25
                if (opp.state in CLIFF_STATES
                        or (near_edge and opp.state in ATTACK_STATES)):
                    self._challenged = True
            if prev is not None and curr.damage > prev.damage + 0.01:
                self._challenged = True
                if opp is not None:
                    mv = attack_name(opp.last_attack)
                    if mv:
                        self._last_hit_move = mv

            if offstage:
                # Detect double jump use while offstage
                if self._prev_jumps is not None and curr.jumps < self._prev_jumps:
                    self._dj_used = True
                self._prev_jumps = curr.jumps

                # After double jump, detect when recovery action is initiated.
                # Recovery actions (in priority order):
                #   1. Airdodge (ESCAPE_AIR)
                #   2. Helpless fall after up-B (FALL_SPECIAL*)
                #   3. Aerial thrown after double jump (ATTACK_AIR_*) — e.g. Ness
                RECOVERY_ACTIONS = (
                    ActionState.ESCAPE_AIR,
                    ActionState.FALL_SPECIAL,
                    ActionState.FALL_SPECIAL_F,
                    ActionState.FALL_SPECIAL_B,
                )
                if self._dj_used and self._recovery_y is None:
                    entering_recovery = (
                        curr.state in RECOVERY_ACTIONS
                        and self._prev_state not in RECOVERY_ACTIONS
                    )
                    entering_aerial = (
                        curr.state in ATTACK_AIR_STATES
                        and self._prev_state not in ATTACK_AIR_STATES
                    )
                    if entering_recovery or entering_aerial:
                        self._recovery_y = curr.y

                self._prev_state = curr.state
            else:
                self._prev_jumps = None

            if self._is_safe_on_stage(curr):
                self._close(converted=False)

    def notify_death(self):
        if self._active:
            self._close(converted=True)

    def _close(self, converted):
        # Categorize by Y when recovery action was initiated after double jump.
        # If no recovery action detected (returned with jump height alone) → above.
        if self._recovery_y is not None:
            category = "below" if self._recovery_y < -5 else "above"
        else:
            category = "above"
        self.situations.append({
            "frame":      self._start,
            "category":   category,
            "converted":  converted,
            "challenged": self._challenged,
            "finish":     self._last_hit_move if converted else None,
        })
        self._active = False

    def finalize(self):
        if self._active:
            self._close(converted=False)

    def summary(self):
        result = {
            "above": {"attempts": 0, "conversions": 0},
            "below": {"attempts": 0, "conversions": 0},
            "challenged": 0,   # recoveries the edgeguarder contested at all
            "free": 0,         # got back with zero contest (free recovery)
            "finish_moves": {},  # what ended converted edgeguards (no hit = edgehog)
        }
        for s in self.situations:
            cat = s["category"]
            if cat in result:
                result[cat]["attempts"] += 1
                if s["converted"]:
                    result[cat]["conversions"] += 1
            if s.get("challenged"):
                result["challenged"] += 1
            elif not s["converted"]:
                result["free"] += 1
            if s["converted"]:
                mv = s.get("finish") or "edgehog"
                result["finish_moves"][mv] = result["finish_moves"].get(mv, 0) + 1
        return result


# ---------------------------------------------------------------------------
# 3b. State History + Neutral Event Classifier
# ---------------------------------------------------------------------------

NEUTRAL_LOOKBACK = 20  # frames to look back when classifying neutral events
ATTACK_LOOKBACK  = 8   # shorter window for identifying attacker's own recent action

# Loser contexts where the victim's own attack created the opening, so the
# specific move can be named from their state history.
ATTACK_LOSER_CONTEXTS = frozenset({
    "whiffed", "attacked_into_shield", "attacked_cc_grabbed", "reversal_victim",
})
# "Attacker was recovering" hint for reversals: helpless up-B fall or ledge
# states in their recent history mean the victim's failed hit was an edgeguard
# attempt, not an onstage combo extension. ESCAPE_AIR is deliberately excluded
# (an onstage combo victim airdodging out would false-positive).
EDGE_RECOVERY_HINT = frozenset({
    ActionState.FALL_SPECIAL, ActionState.FALL_SPECIAL_F, ActionState.FALL_SPECIAL_B,
}) | frozenset(CLIFF_STATES)

class StateHistory:
    """Rolling 60-frame window of ActionState for one port."""
    def __init__(self, maxlen=60):
        self._buf = deque(maxlen=maxlen)

    def push(self, state):
        self._buf.append(state)

    def had_state_in_last(self, state_set, n_frames):
        for s in list(self._buf)[-n_frames:]:
            if s in state_set:
                return True
        return False

    def last_state_in(self, state_set, n_frames):
        """Most recent state from state_set within the last n_frames, or None."""
        for s in reversed(list(self._buf)[-n_frames:]):
            if s in state_set:
                return s
        return None


def _classify_neutral_event(opener_type, victim_hist, attacker_hist):
    """
    Jointly classify both players' context at the start of a punish sequence.
    Returns (loser_context, winner_context, is_continuation).

    opener_type: "grab", "launch", "knockdown", or None
    victim_hist / attacker_hist: StateHistory objects (may be None for edge cases)
    """
    if victim_hist is None or attacker_hist is None:
        return ("unknown", "unknown", opener_type == "knockdown")

    if opener_type == "knockdown":
        return ("missed_tech", "tech_punish", True)

    if opener_type == "grab":
        victim_attacked = victim_hist.had_state_in_last(ATTACK_STATES, NEUTRAL_LOOKBACK)
        winner_shielded = attacker_hist.had_state_in_last(SHIELD_STATES, NEUTRAL_LOOKBACK)
        winner_crouched = attacker_hist.had_state_in_last(SQUAT_STATES, NEUTRAL_LOOKBACK)
        if victim_attacked and winner_shielded:
            return ("attacked_into_shield", "oos_grab", False)
        if victim_attacked and winner_crouched:
            return ("attacked_cc_grabbed", "cc_grab", False)
        # Distinguish how the grab was set up
        if attacker_hist.had_state_in_last(DASH_STATES, ATTACK_LOOKBACK):
            return ("grabbed_neutral", "dash_grab", False)
        return ("grabbed_neutral", "walk_grab", False)

    if opener_type == "launch":
        if victim_hist.had_state_in_last(frozenset({ActionState.ESCAPE_AIR}), NEUTRAL_LOOKBACK):
            return ("airdodged", "airdodge_punish", False)
        if victim_hist.had_state_in_last(LANDING_STATES, NEUTRAL_LOOKBACK):
            return ("landing_lag", "landing_punish", False)
        if victim_hist.had_state_in_last(ATTACK_STATES, NEUTRAL_LOOKBACK):
            # Attacker was recently tumbling → victim was trying to extend a punish and got reversed
            if attacker_hist.had_state_in_last(DAMAGE_FLY_STATES, NEUTRAL_LOOKBACK):
                return ("reversal_victim", "reversal_winner", False)
            return ("whiffed", "whiff_punish", False)
        # Break down the catch-all by what the winner was doing
        if attacker_hist.had_state_in_last(ATTACK_AIR_STATES, ATTACK_LOOKBACK):
            return ("caught_neutral", "aerial_approach", False)
        if attacker_hist.had_state_in_last(frozenset({ActionState.ATTACK_DASH}), ATTACK_LOOKBACK):
            return ("caught_neutral", "dash_attack", False)
        if attacker_hist.had_state_in_last(GROUND_ATTACK_STATES, ATTACK_LOOKBACK):
            return ("caught_neutral", "ground_attack", False)
        return ("caught_neutral", "approach", False)

    # opener is None (multi-hit, no clear single opener)
    if attacker_hist.had_state_in_last(ATTACK_AIR_STATES, ATTACK_LOOKBACK):
        return ("caught_neutral", "aerial_approach", False)
    if attacker_hist.had_state_in_last(frozenset({ActionState.ATTACK_DASH}), ATTACK_LOOKBACK):
        return ("caught_neutral", "dash_attack", False)
    if attacker_hist.had_state_in_last(GROUND_ATTACK_STATES, ATTACK_LOOKBACK):
        return ("caught_neutral", "ground_attack", False)
    return ("caught_neutral", "approach", False)


# ---------------------------------------------------------------------------
# 4. Neutral Tracker
# ---------------------------------------------------------------------------

class NeutralTracker:
    def __init__(self):
        self.crouch_frames = 0
        self.shield_frames = 0

    def feed(self, curr):
        if curr.state in (ActionState.SQUAT, ActionState.SQUAT_WAIT, ActionState.SQUAT_RV):
            self.crouch_frames += 1
        if curr.state in SHIELD_STATES:
            self.shield_frames += 1


# ---------------------------------------------------------------------------
# 4a. Out-of-Shield Tracker  (response + speed after taking a hit on shield)
# ---------------------------------------------------------------------------

OOS_CATEGORIES = ("grab", "usmash", "jump", "shielddrop", "roll", "spotdodge",
                  "drop", "grabbed", "hit", "other")
_PASS_STATE = getattr(ActionState, "PASS", None)  # platform shield-drop


class OOSTracker:
    """What a player does after taking a hit on shield, and how fast.

    A sample starts on each GUARD_SET_OFF (shieldstun) edge. Waiting is
    counted from the end of shieldstun (the first actionable-ish frame) until
    the player leaves the shield family, and the exit state names the
    response:
      grab / usmash / jump (incl. nair-OOS + wavedash-OOS starts) /
      shielddrop (platform drop-through) / roll / spotdodge /
      drop (released shield) / grabbed / hit (poked or grab beat the shield) /
      other
    """
    TIMEOUT = 120  # give up and call it "drop" after this many waiting frames

    def __init__(self):
        self.samples    = 0   # shield hits taken
        self.resolved   = 0
        self.total_wait = 0   # post-stun frames spent holding shield before acting
        self.cat_counts = {c: 0 for c in OOS_CATEGORIES}
        self._mode = "IDLE"   # IDLE | STUN | WAIT
        self._wait = 0
        self._prev_state = None

    @staticmethod
    def _categorize(state):
        if state in (ActionState.CATCH, ActionState.CATCH_DASH):
            return "grab"
        if state == ActionState.ATTACK_HI_4:
            return "usmash"
        if state == ActionState.KNEE_BEND:
            return "jump"
        if _PASS_STATE is not None and state == _PASS_STATE:
            return "shielddrop"
        if state in (ActionState.ESCAPE_F, ActionState.ESCAPE_B):
            return "roll"
        if state == ActionState.ESCAPE:
            return "spotdodge"
        if state in CAPTURE_STATES:
            return "grabbed"
        if state in GOT_HIT_STATES:
            return "hit"
        return "other"

    def _resolve(self, category):
        self.resolved   += 1
        self.total_wait += self._wait
        self.cat_counts[category] += 1
        self._mode = "IDLE"
        self._wait = 0

    def feed(self, curr):
        state = curr.state
        prev  = self._prev_state
        self._prev_state = state

        if state == ActionState.GUARD_SET_OFF:
            if prev != ActionState.GUARD_SET_OFF:
                # new shield hit; if one was pending, its clock just restarts
                self.samples += 1
            self._mode = "STUN"
            self._wait = 0
            return

        if self._mode == "IDLE":
            return

        if state in SHIELD_STATES and state != ActionState.GUARD_OFF:
            self._mode = "WAIT"
            self._wait += 1
            if self._wait > self.TIMEOUT:
                self._resolve("drop")
            return

        # left the shield family (or released it): name the response
        self._resolve("drop" if state == ActionState.GUARD_OFF
                      else self._categorize(state))

    def summary(self):
        return {
            "samples":    self.samples,
            "resolved":   self.resolved,
            "total_wait": self.total_wait,
            "categories": dict(self.cat_counts),
        }


# ---------------------------------------------------------------------------
# 4b. Move Safety Tracker  (per-move outcome / punished-rate / spacing proxy)
# ---------------------------------------------------------------------------

MOVE_SAFETY_PUNISH_WINDOW = 45  # frames after starting a move in which getting
                                # hit or grabbed counts as that move being punished


class MoveSafetyTracker:
    """Per-move usage + safety for one player's NORMALS (jab/tilts/smashes/
    dash attack/aerials). Specials use char-specific action states and are
    skipped, as in ATTACK_STATE_NAME_MAP.

    Each use records:
      outcome  - hit / shield (opponent took shieldstun) / whiff
      punished - the player entered hit/grab states within
                 MOVE_SAFETY_PUNISH_WINDOW frames of starting the move
                 (split by the move's own outcome: a punished hit = they
                 CC'd/traded, a punished shield = shield-grabbed, ...)
      dist     - distance to the opponent at startup. A spacing proxy only
                 (replays carry no hitbox data): compare where a move starts
                 when it HITS vs when it gets PUNISHED instead of reading
                 the absolute numbers.
    """
    def __init__(self):
        self.moves = {}   # move name -> stat dict
        self._cur  = None # latest use; doubles as the punish-attribution target
        self._opp_prev_damage = None
        self._opp_prev_state  = None

    @staticmethod
    def _blank():
        return {"n": 0, "hit": 0, "shield": 0, "whiff": 0,
                "punished_hit": 0, "punished_shield": 0, "punished_whiff": 0,
                "dist_sum": 0.0, "hit_dist_sum": 0.0, "whiff_dist_sum": 0.0,
                "punished_dist_sum": 0.0}

    def _bank(self, c):
        """Record the finished use's outcome counts + distance sums."""
        s = self.moves.setdefault(c["move"], self._blank())
        s[c["outcome"]] += 1
        if c["outcome"] == "hit":
            s["hit_dist_sum"] += c["dist"]
        elif c["outcome"] == "whiff":
            s["whiff_dist_sum"] += c["dist"]
        c["open"] = False

    def feed(self, frame_idx, curr, prev, opp):
        prev_state = prev.state if prev else None
        in_attack  = curr.state in ATTACK_STATES
        was_attack = prev_state in ATTACK_STATES

        # Resolve the in-progress use: upgrade its outcome while the move is
        # out, bank it on the first frame after the attack state ends.
        if self._cur is not None and self._cur["open"]:
            c = self._cur
            if in_attack:
                if (opp is not None and self._opp_prev_damage is not None
                        and opp.damage > self._opp_prev_damage + 0.01):
                    c["outcome"] = "hit"   # hit beats shield
                elif (c["outcome"] == "whiff" and opp is not None
                        and opp.state == ActionState.GUARD_SET_OFF
                        and self._opp_prev_state != ActionState.GUARD_SET_OFF):
                    c["outcome"] = "shield"
            else:
                self._bank(c)

        # Punished? Entering hit/grab states shortly after the move started.
        # Runs after banking, so c["outcome"] is final when this fires.
        if (self._cur is not None and not self._cur["punished"]
                and not self._cur["open"]
                and frame_idx - self._cur["start"] <= MOVE_SAFETY_PUNISH_WINDOW
                and curr.state in GOT_HIT_STATES
                and prev_state not in GOT_HIT_STATES):
            c = self._cur
            s = self.moves.setdefault(c["move"], self._blank())
            s["punished_" + c["outcome"]] += 1
            s["punished_dist_sum"] += c["dist"]
            c["punished"] = True

        # New use: transition into an attack state. A later use overwrites
        # _cur, so a punish always attributes to the newest move.
        if in_attack and not was_attack:
            move = ATTACK_STATE_NAME_MAP.get(curr.state)
            if move is not None:
                dist = 0.0
                if opp is not None:
                    dist = ((curr.x - opp.x) ** 2 + (curr.y - opp.y) ** 2) ** 0.5
                s = self.moves.setdefault(move, self._blank())
                s["n"] += 1
                s["dist_sum"] += dist
                self._cur = {"move": move, "start": frame_idx, "dist": dist,
                             "outcome": "whiff", "open": True, "punished": False}

        if opp is not None:
            self._opp_prev_damage = opp.damage
            self._opp_prev_state  = opp.state

    def finalize(self):
        if self._cur is not None and self._cur["open"]:
            self._bank(self._cur)


# ---------------------------------------------------------------------------
# 5. Punish Tracker  (tracks punishes received by one player)
# ---------------------------------------------------------------------------

class PunishTracker:
    """
    Tracks punish sequences on a victim, but only from real openings:
      grab       - victim was in CAPTURE/THROWN state at sequence start
      knockdown  - victim was in DOWN state (tech situation)
      launch     - victim enters DAMAGE_FLY from a non-damage state (knocked into tumble)

    Single neutral pokes with one hit are excluded (opener=None, hits=1).
    A sequence with >= 2 hits always counts regardless of opener.

    Outcome tagging:
      kill      - victim's stock decreases
      edgeguard - punish closes with victim offstage
      reset     - victim back on stage
    """
    def __init__(self, ledge_x):
        self.ledge_x   = ledge_x
        self.sequences = []

        self._active         = False
        self._start_pct      = 0.0
        self._start_frame    = 0
        self._hits           = 0
        self._peak_pct       = 0.0
        self._last_dmg_frame = -999
        self._last_disadv_frame = -999  # last frame victim was hit/grabbed/thrown/down
        self._last_in_hit    = False
        self._opener         = None   # "grab", "knockdown", "launch", or None
        self._opener_move    = None   # specific move that opened (e.g. "down_special")
        self._ender_move     = None   # last move landed before the sequence closed
        self._prev_state     = None   # victim state from previous frame
        self._loser_context  = "unknown"
        self._winner_context = "unknown"
        self._is_continuation = False
        self._hit_log        = []     # [move_or_None, victim_pct] per counted hit
        self._loser_move     = None   # victim's own move that created the opening
        self._reversal_kind  = None   # "edgeguard_try"/"combo_extension" on reversals

    def feed(self, frame_idx, victim, victim_hist=None, attacker_hist=None,
             attacker_attack=None):
        state  = victim.state
        in_dmg = _sv(state) in DAMAGE_STATES
        # A "hit" includes throws: at tech-chase percents a dthrow puts the victim
        # into THROWN -> DOWN states without ever entering a DAMAGE state, so
        # keying only on damage states missed most throws and whole tech-chases.
        in_hit = in_dmg or state in THROWN_STATES
        # "Disadvantage" = victim is being hit/thrown/grabbed/knocked-down/teching.
        # Keeping all of these alive lets a chaingrab or tech-chase (the victim
        # never returns to neutral) stay ONE string instead of splitting.
        in_disadv = (in_hit or state in DAMAGE_FLY_STATES or state in CAPTURE_STATES
                     or state in KNOCKDOWN_TECH_STATES)
        # Name a throw from the victim's THROWN state; otherwise from the
        # attacker's last landed attack.
        move = THROWN_NAME_MAP.get(state) if state in THROWN_STATES else attack_name(attacker_attack)

        if in_hit:
            # New opening only when there's no live punish, or the victim had
            # returned to neutral (out of disadvantage) for a real gap. A regrab
            # or tech-chase regrab keeps _last_disadv_frame fresh, extending it.
            if not self._active or frame_idx - self._last_disadv_frame > 60:
                if self._active:
                    self._close(frame_idx, victim, killed=False)

                # Classify opener from victim's pre-hit state
                prev = self._prev_state
                if prev in CAPTURE_STATES or prev in THROWN_STATES:
                    opener = "grab"
                elif prev in KNOCKDOWN_TECH_STATES:
                    opener = "knockdown"
                elif state in DAMAGE_FLY_STATES and (prev is None or _sv(prev) not in DAMAGE_STATES):
                    opener = "launch"
                else:
                    opener = None

                loser_ctx, winner_ctx, is_cont = _classify_neutral_event(
                    opener, victim_hist, attacker_hist
                )

                # Name the victim's own move when their attack created the opening.
                loser_move = None
                if loser_ctx in ATTACK_LOSER_CONTEXTS and victim_hist is not None:
                    ls = victim_hist.last_state_in(ATTACK_STATES, NEUTRAL_LOOKBACK)
                    loser_move = ATTACK_STATE_NAME_MAP.get(ls)
                # Reversal context: failed edgeguard attempt vs onstage combo
                # extension. Either the victim was offstage when reversed, or
                # the attacker had recently been in recovery/ledge states.
                reversal_kind = None
                if loser_ctx == "reversal_victim":
                    offstage   = abs(victim.x) > self.ledge_x
                    recovering = (attacker_hist is not None and
                                  attacker_hist.had_state_in_last(EDGE_RECOVERY_HINT, 45))
                    reversal_kind = ("edgeguard_try" if (offstage or recovering)
                                     else "combo_extension")

                self._active          = True
                self._opener          = opener
                self._opener_move     = move
                self._ender_move      = move
                self._loser_context   = loser_ctx
                self._winner_context  = winner_ctx
                self._is_continuation = is_cont
                self._loser_move      = loser_move
                self._reversal_kind   = reversal_kind
                self._start_pct       = victim.damage
                self._start_frame     = frame_idx
                self._hits            = 1
                self._peak_pct        = victim.damage
                self._hit_log         = [[move, round(victim.damage, 1)]]
            elif not self._last_in_hit:
                self._hits += 1
                self._hit_log.append([move, round(victim.damage, 1)])
            elif victim.damage > self._peak_pct + 0.01:
                # New hit while the victim is STILL in hitstun (a true combo):
                # in_hit never drops, but percent only rises on a fresh hit.
                # Only log when the move name CHANGES — a repeat name during
                # continuous hitstun is multi-part damage of ONE move (a
                # throw's hit+release components, drill/needle ticks), since a
                # real same-move re-hit passes through a hitstun exit and is
                # caught by the re-entry branch above. Feeds hit_moves only —
                # legacy `hits` stays a re-entry count so the 1-hit-poke
                # exclusion is unchanged.
                if move is not None and self._hit_log and move != self._hit_log[-1][0]:
                    self._hit_log.append([move, round(victim.damage, 1)])
            if move is not None:
                self._ender_move = move
                if self._hit_log:
                    # last_attack_landed can lag the hit by a frame — backfill
                    # the newest hit entry once the move name resolves.
                    self._hit_log[-1][0] = move
            self._last_dmg_frame = frame_idx
            self._peak_pct = max(self._peak_pct, victim.damage)
        else:
            # Close only after the victim has been fully out of disadvantage
            # (not grabbed, not teching) for the grace window — so a slow regrab
            # or tech-chase regrab doesn't prematurely end the string.
            if self._active and frame_idx - self._last_disadv_frame > 90:
                self._close(frame_idx, victim, killed=False)

        if in_disadv:
            self._last_disadv_frame = frame_idx
        self._last_in_hit = in_hit
        self._prev_state  = state

    def notify_death(self, frame_idx):
        if self._active:
            self._close(frame_idx, None, killed=True)

    def _close(self, frame_idx, victim, killed):
        dmg = self._peak_pct - self._start_pct
        is_real_punish = (self._opener is not None) or (self._hits >= 2)
        if dmg > 0 and is_real_punish:
            if killed:
                outcome = "kill"
            elif victim and abs(victim.x) > self.ledge_x:
                outcome = "edgeguard"
            else:
                outcome = "reset"
            self.sequences.append({
                "frame":          self._start_frame,
                "time":           frames_to_time(self._start_frame),
                "damage":         round(dmg, 1),
                "start_pct":      round(self._start_pct, 1),
                "end_pct":        round(self._peak_pct, 1),
                "hits":           self._hits,
                "hit_moves":      [list(h) for h in self._hit_log],
                "opener":         self._opener,
                "opener_move":    self._opener_move,
                "ender_move":     self._ender_move,
                "outcome":        outcome,
                "loser_context":  self._loser_context,
                "winner_context": self._winner_context,
                "loser_move":     self._loser_move,
                "reversal_kind":  self._reversal_kind,
                "is_continuation": self._is_continuation,
            })
        self._active = False

    def finalize(self, frame_idx, last_victim_pf):
        if self._active:
            self._close(frame_idx, last_victim_pf, killed=False)


# ---------------------------------------------------------------------------
# Death Tracker
# ---------------------------------------------------------------------------

class DeathTracker:
    def __init__(self):
        self.deaths       = []
        self._last_stocks = None
        self._stock_start = None
        self._start_pct   = 0.0
        self._peak_pct    = 0.0

    def feed(self, frame_idx, pf):
        stocks = pf.stocks
        died   = (self._last_stocks is not None and stocks < self._last_stocks)

        if died and self._stock_start is not None:
            self.deaths.append({
                "stock":    len(self.deaths) + 1,
                "frame":    frame_idx,
                "time":     frames_to_time(frame_idx),
                "dmg_taken": round(self._peak_pct - self._start_pct, 1),
            })
            self._stock_start = None
            self._peak_pct    = 0.0

        if stocks > 0:
            if self._stock_start is None:
                self._stock_start = frame_idx
                self._start_pct   = pf.damage
                self._peak_pct    = pf.damage
            else:
                self._peak_pct = max(self._peak_pct, pf.damage)

        self._last_stocks = stocks
        return died


LEDGE_COVERAGE_WINDOW = 75  # frames after a ledge option in which a punish
                            # sequence opening counts as covering that option


def _ledge_coverage(opp_ledge_events, my_seqs_on_opp):
    """{option: {n, punished}} — for each of the opponent's ledge options, did
    one of my punish sequences on them open within the follow-up window?"""
    cov = {}
    seq_frames = sorted(s["frame"] for s in my_seqs_on_opp)
    for f, opt in opp_ledge_events:
        slot = cov.setdefault(opt, {"n": 0, "punished": 0})
        slot["n"] += 1
        if any(f <= sf <= f + LEDGE_COVERAGE_WINDOW for sf in seq_frames):
            slot["punished"] += 1
    return cov


# ---------------------------------------------------------------------------
# Game Analyzer
# ---------------------------------------------------------------------------

class GameAnalyzer:
    def __init__(self, slp_path):
        self.game        = Game(slp_path)
        self.start       = self.game.start
        stage            = self.start.stage
        self.stage_data  = STAGE_DATA.get(stage, DEFAULT_STAGE_DATA)
        self.stage_name  = stage.name.replace("_", " ").title() if stage else "Unknown"

        self.active_ports  = [(i, p) for i, p in enumerate(self.start.players) if p is not None]
        self.char_names    = {i: character_name(p) for i, p in self.active_ports}
        self.port_indices  = [i for i, _ in self.active_ports]
        self.start_stocks  = {i: p.stocks for i, p in self.active_ports}
        self.netplay       = get_netplay_info(slp_path)

        sd = self.stage_data
        self.tech    = {i: TechSkillTracker()                       for i in self.port_indices}
        self.ledgetech = {i: LedgeTechTracker(sd["ledge_x"])        for i in self.port_indices}
        self.postland = {i: PostLandingTracker()                    for i in self.port_indices}
        self.stgctrl = {i: StageControlTracker(sd["center_x"])      for i in self.port_indices}
        self.neutral = {i: NeutralTracker()                         for i in self.port_indices}
        self.oos     = {i: OOSTracker()                             for i in self.port_indices}
        self.movesafety = {i: MoveSafetyTracker()                   for i in self.port_indices}
        self.deaths  = {i: DeathTracker()                           for i in self.port_indices}
        # punishes[i] = punishes received by player i
        self.punishes = {i: PunishTracker(sd["ledge_x"])            for i in self.port_indices}
        # edgeguards[i] = recovery situations for player i (i.e. their opponent is edgeguarding them)
        self.edgeguards = {i: EdgeguardTracker(sd["ledge_x"], sd["floor_y"]) for i in self.port_indices}

        # opponent map and per-port state history for neutral classifier
        if len(self.port_indices) == 2:
            a, b = self.port_indices
            self.opponent = {a: b, b: a}
        else:
            self.opponent = {i: i for i in self.port_indices}
        self.state_hist = {i: StateHistory() for i in self.port_indices}

        # Self-destructs + per-death geography bucket (sd/edgehog/side/top)
        self.sd_counts     = {i: 0  for i in self.port_indices}
        self.death_buckets = {i: [] for i in self.port_indices}

        self._prev = {i: None for i in self.port_indices}
        self._last_pf = {i: None for i in self.port_indices}

    def _is_self_destruct(self, port_idx, opp_idx):
        """A self-destruct = the opponent did nothing to cause the death: no
        recent knockback/grab, and the opponent wasn't edgeguarding (ledge-hang
        or a hitbox near the edge). Evaluated at death-state ENTRY, so the
        lookback covers the real pre-death frames.

        Being offstage ("recovering") is NOT itself a disqualifier — most SDs
        happen while offstage. The old `was_recovering` guard worked when death
        was read at the stock decrement, but at dead-entry the dying player is
        still flagged as recovering, so it suppressed every genuine SD. Instead a
        gimp is excluded by three checks: recent knockback, the opponent actively
        edgeguarding, and the recovery tracker's own context (whether THIS
        offstage trip was challenged or began from a knockback)."""
        vh = self.state_hist[port_idx]
        if vh.had_state_in_last(GOT_HIT_STATES, 90):   # knocked / grabbed into it
            return False
        oh = self.state_hist.get(opp_idx)
        if oh is not None and (oh.had_state_in_last(CLIFF_STATES, 40)
                               or oh.had_state_in_last(ATTACK_STATES, 30)):
            return False
        # The recovery tracker knows whether this specific offstage trip was the
        # opponent's doing: a hit that knocked you off (`_last_hit_move`) or any
        # contest while out there (`_challenged`). Either => gimp, not an SD.
        eg = self.edgeguards.get(port_idx)
        if eg is not None and eg._active and (eg._challenged or eg._last_hit_move is not None):
            return False
        return True

    def _classify_death(self, dead_state, is_sd, hit_recent):
        """Death geography from the victim's death action-state — the game names
        the KO direction directly, so no blast-zone coords or position heuristics:
          sd      - self-destruct (no opponent involvement)
          top     - DeadUp* (3-10): launched out the top (star / screen KO)
          side    - DeadLeft/Right (1/2): launched out the side
          edgehog - DeadDown (0), or any death WITHOUT recent knockback (gimp,
                    ledge-hog, walled-out / missed recovery, low spike)
        `hit_recent` = victim was in knockback/hitstun just before dying."""
        if is_sd:
            return "sd"
        if not hit_recent:
            return "edgehog"
        s = _sv(dead_state)
        if s in DEAD_SIDE_STATES:
            return "side"
        if s in DEAD_BOTTOM_STATES:
            return "edgehog"   # off the bottom = spike / gimp / edge situation
        return "top"           # DeadUp* (3-10)

    def run(self):
        frames = self.game.frames
        for frame_idx, frame in enumerate(frames):
            pfs = {}
            for port_idx in self.port_indices:
                port = frame.ports[port_idx]
                if port is None or port.leader is None:
                    continue
                pfs[port_idx] = PF(port.leader.pre, port.leader.post)

            for port_idx in self.port_indices:
                if port_idx not in pfs:
                    continue
                curr = pfs[port_idx]
                prev = self._prev[port_idx]
                opp_idx = self.opponent.get(port_idx, port_idx)

                # Per-stock damage bookkeeping still keys off the stock count.
                self.deaths[port_idx].feed(frame_idx, curr)
                # Death geography / kill credit is handled on ENTRY to a Dead
                # action-state (see DEAD_STATES). At that frame the launch is the
                # previous frame, so the punish is still live (correct kill
                # credit), the self-destruct lookback sees the real hit, and the
                # death state names the blast zone (correct top/side geography).
                entered_dead = (_sv(curr.state) in DEAD_STATES
                                and (prev is None or _sv(prev.state) not in DEAD_STATES))
                if entered_dead:
                    is_sd = self._is_self_destruct(port_idx, opp_idx)
                    if is_sd:
                        self.sd_counts[port_idx] += 1
                    hit_recent = self.state_hist[port_idx].had_state_in_last(
                        DAMAGE_FLY_STATES | set(DAMAGE_STATES), 15)
                    self.death_buckets[port_idx].append(
                        self._classify_death(curr.state, is_sd, hit_recent))
                    self.edgeguards[port_idx].notify_death()
                    self.punishes[port_idx].notify_death(frame_idx)

                if curr.stocks == 0:
                    self._prev[port_idx] = curr
                    self._last_pf[port_idx] = curr
                    continue

                self.tech[port_idx].feed(curr)
                self.ledgetech[port_idx].feed(frame_idx, curr)
                self.postland[port_idx].feed(curr)
                self.stgctrl[port_idx].feed(curr)
                self.neutral[port_idx].feed(curr)
                self.oos[port_idx].feed(curr)
                if opp_idx != port_idx:  # meaningless without a real opponent (FFA)
                    self.movesafety[port_idx].feed(
                        frame_idx, curr, prev, pfs.get(opp_idx))
                self.punishes[port_idx].feed(
                    frame_idx, curr,
                    victim_hist=self.state_hist[port_idx],
                    attacker_hist=self.state_hist.get(opp_idx),
                    attacker_attack=(pfs[opp_idx].last_attack if opp_idx in pfs else None),
                )
                self.edgeguards[port_idx].feed(
                    frame_idx, curr, prev,
                    opp=(pfs.get(opp_idx) if opp_idx != port_idx else None))
                # push state AFTER trackers have consumed it so history lags current frame
                self.state_hist[port_idx].push(curr.state)

                self._prev[port_idx] = curr
                self._last_pf[port_idx] = curr

        last_frame = len(frames) - 1
        for port_idx in self.port_indices:
            last_pf = self._last_pf[port_idx]
            self.punishes[port_idx].finalize(last_frame, last_pf)
            self.edgeguards[port_idx].finalize()
            self.movesafety[port_idx].finalize()

    def build_data(self):
        """Return structured dict of all analysis results."""
        def _count_contexts(seqs, key, neutral_only=False):
            counts = {}
            for s in seqs:
                if neutral_only and s.get("is_continuation"):
                    continue
                label = s.get(key, "unknown")
                counts[label] = counts.get(label, 0) + 1
            return counts
        # edgeguards[i] tracks player i's recovery situations (i.e. the opponent edgeguarding i).
        # For each player's report section, we want to show their edgeguard opportunities,
        # which means showing the *opponent's* recovery situations.
        opponent = {}
        if len(self.port_indices) == 2:
            a, b = self.port_indices
            opponent[a] = b
            opponent[b] = a
        else:
            # FFA / other: no clear opponent, fall back to own data
            for i in self.port_indices:
                opponent[i] = i

        ports = {}
        for port_idx in self.port_indices:
            t  = self.tech[port_idx]
            lt = self.ledgetech[port_idx]
            sc = self.stgctrl[port_idx]
            n  = self.neutral[port_idx]
            d  = self.deaths[port_idx]
            p  = self.punishes[port_idx]
            eg = self.edgeguards[opponent[port_idx]]  # opponent's recovery = my edgeguard opps

            seqs     = p.sequences
            opp_seqs = self.punishes[opponent[port_idx]].sequences
            start_stk = self.start_stocks.get(port_idx, 4)
            nl        = self.netplay.get(port_idx, {})
            ports[port_idx] = {
                "char":         self.char_names[port_idx],
                "label":        port_label(port_idx, self.char_names[port_idx]),
                "stocks_lost":  len(d.deaths),
                "start_stocks": start_stk,
                "won":          len(d.deaths) < start_stk,
                "netplay_code": nl.get("code", ""),
                "netplay_name": nl.get("name", ""),
                "deaths":       d.deaths,
                "sd_count":     self.sd_counts[port_idx],
                "death_buckets": list(self.death_buckets[port_idx]),
                # My own recovery situations (offstage): attempts vs how many ended in death.
                "recovery": (lambda s: {
                    "attempts": s["above"]["attempts"] + s["below"]["attempts"],
                    "deaths":   s["above"]["conversions"] + s["below"]["conversions"],
                })(self.edgeguards[port_idx].summary()),
                "tech_skill": {
                    "l_cancel_attempts": t.l_cancel_attempts,
                    "l_cancel_success":  t.l_cancel_success,
                    "l_cancel_rate":     pct(t.l_cancel_success, t.l_cancel_attempts),
                    "high_aerials":      t.high_aerials,
                    "low_aerials":       t.low_aerials,
                    "wd_attempts":       t.wd_attempts,
                    "wd_perfect":        t.wd_perfect,
                    "wd_rate":           pct(t.wd_perfect, t.wd_attempts),
                    "f1_attempts":       t.f1_attempts,
                    "f1_perfect":        t.f1_perfect,
                    "f1_rate":           pct(t.f1_perfect, t.f1_attempts),
                    # Per-aerial breakdowns
                    "lc_att_by_aerial":  dict(t.lc_att_by_aerial),
                    "lc_suc_by_aerial":  dict(t.lc_suc_by_aerial),
                    "high_by_aerial":    dict(t.high_by_aerial),
                    "low_by_aerial":     dict(t.low_by_aerial),
                    "f1_att_by_aerial":  dict(t.f1_att_by_aerial),
                    "f1_prf_by_aerial":  dict(t.f1_prf_by_aerial),
                },
                "ledge_tech": lt.summary(),
                "post_landing": self.postland[port_idx].summary(),
                "stage_control": {
                    "center_frames": sc.center_frames,
                    "total_frames":  sc.total_frames,
                    "center_pct":    sc.center_pct(),
                },
                "neutral": {
                    "crouch_frames":  n.crouch_frames,
                    "crouch_seconds": n.crouch_frames / FPS,
                    "shield_frames":  n.shield_frames,
                    "shield_seconds": n.shield_frames / FPS,
                },
                "move_usage": {m: dict(s) for m, s in
                               self.movesafety[port_idx].moves.items()},
                "oos": self.oos[port_idx].summary(),
                # Opponent's ledge options + whether I converted an opening
                # within the follow-up window of each one (ledge coverage).
                "ledge_coverage": _ledge_coverage(
                    self.ledgetech[opponent[port_idx]].events,
                    self.punishes[opponent[port_idx]].sequences,
                ) if opponent[port_idx] != port_idx else {},
                "punishes": {
                    "sequences":       seqs,
                    "count":           len(seqs),
                    "avg_damage":      sum(s["damage"] for s in seqs) / len(seqs) if seqs else 0.0,
                    "avg_damage_dealt": sum(s["damage"] for s in opp_seqs) / len(opp_seqs) if opp_seqs else 0.0,
                    "kills":           sum(1 for s in seqs if s["outcome"] == "kill"),
                    "edgeguards":      sum(1 for s in seqs if s["outcome"] == "edgeguard"),
                    "resets":          sum(1 for s in seqs if s["outcome"] == "reset"),
                    # neutral analysis — from victim's perspective (I lost these)
                    "neutral_losses":      sum(1 for s in seqs if not s.get("is_continuation")),
                    "continuations":       sum(1 for s in seqs if s.get("is_continuation")),
                    "neutral_loss_by":     _count_contexts(seqs, "loser_context",  neutral_only=True),
                    # neutral analysis — from attacker's perspective (I won these, seqs = opp_seqs)
                    "neutral_wins":        sum(1 for s in opp_seqs if not s.get("is_continuation")),
                    "neutral_win_by":      _count_contexts(opp_seqs, "winner_context", neutral_only=True),
                },
                "edgeguard": eg.summary(),
            }

        return {
            "stage":      self.stage_name,
            "stage_data": self.stage_data,
            "ports":      ports,
            "port_order": self.port_indices,
        }


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def _na(val, fmt=".1f", suffix=""):
    if val is None:
        return "N/A"
    return f"{val:{fmt}}{suffix}"

def _pct_flag(rate, lo=70, hi=90):
    if rate < lo:   return f"  [!] Low"
    if rate < hi:   return f"  [~] OK"
    return              f"  [ok] Good"

def _format_postland_categories(cats, total, min_pct=5):
    """Render a sorted, top-categories string like 'shield 31% / jab 17% / ...'.
    Categories below `min_pct`% are lumped into 'other'.
    """
    if total <= 0:
        return ""
    sorted_cats = sorted(cats.items(), key=lambda kv: -kv[1])
    shown = []
    other_pct = 0.0
    for cat, count in sorted_cats:
        if count == 0:
            continue
        pct_v = 100.0 * count / total
        if cat == "other" or pct_v < min_pct:
            other_pct += pct_v
            continue
        shown.append(f"{cat} {pct_v:.0f}%")
    if other_pct >= 1:
        shown.append(f"other {other_pct:.0f}%")
    return " / ".join(shown)

def format_report(game_data, focus_port=None):
    lines = []
    def out(s=""): lines.append(s)

    stage = game_data["stage"]
    ports = game_data["ports"]
    order = game_data["port_order"]
    if focus_port is not None:
        order = [p for p in order if p == focus_port]

    out("=" * 62)
    out("  SLIPPI VOD REVIEW")
    out("=" * 62)
    out(f"  Stage: {stage}")
    for idx in order:
        out(f"  {ports[idx]['label']}")
    out()

    for port_idx in order:
        p = ports[port_idx]
        ts = p["tech_skill"]
        sc = p["stage_control"]
        nt = p["neutral"]
        pu = p["punishes"]
        eg = p["edgeguard"]

        out("-" * 62)
        out(f"  {p['label']}   (stocks lost: {p['stocks_lost']})")
        out("-" * 62)

        # --- 1. Tech Skill ---
        out()
        out("  [1] TECH SKILL")
        out()

        # L-cancel
        if ts["l_cancel_attempts"] > 0:
            flag = _pct_flag(ts["l_cancel_rate"])
            out(f"    L-cancel        : {ts['l_cancel_success']}/{ts['l_cancel_attempts']}"
                f"  ({ts['l_cancel_rate']:.0f}%){flag}")
            # Per-aerial breakdown
            lc_att = ts["lc_att_by_aerial"]
            lc_suc = ts["lc_suc_by_aerial"]
            high_by = ts["high_by_aerial"]
            for a in AERIALS:
                att = lc_att[a]
                if att == 0 and high_by[a] == 0:
                    continue
                if att > 0:
                    rate = 100.0 * lc_suc[a] / att
                    rate_flag = _pct_flag(rate)
                    suffix = f" (+{high_by[a]} high)" if high_by[a] > 0 else ""
                    out(f"      {a:4s}        : {lc_suc[a]}/{att}  ({rate:.0f}%){rate_flag}{suffix}")
                else:
                    out(f"      {a:4s}        : {high_by[a]} high (autocancel only)")
        else:
            out(f"    L-cancel        : no aerial landings detected")

        # Aerial height split
        total_aerials = ts["high_aerials"] + ts["low_aerials"]
        if total_aerials > 0:
            high_pct = pct(ts["high_aerials"], total_aerials)
            out(f"    Aerial height   : {ts['high_aerials']} high (autocancel) / "
                f"{ts['low_aerials']} low (L-cancel needed)  "
                f"({high_pct:.0f}% high)")
        else:
            out(f"    Aerial height   : no aerials detected")

        # Wavedash
        if ts["wd_attempts"] > 0:
            flag = _pct_flag(ts["wd_rate"])
            out(f"    Wavedash        : {ts['wd_perfect']}/{ts['wd_attempts']}"
                f" perfect  ({ts['wd_rate']:.0f}%){flag}")
        else:
            out(f"    Wavedash        : none detected")

        # Frame-1 aerials
        if ts["f1_attempts"] > 0:
            flag = _pct_flag(ts["f1_rate"])
            out(f"    Frame-1 aerials : {ts['f1_perfect']}/{ts['f1_attempts']}"
                f" on frame 1  ({ts['f1_rate']:.0f}%){flag}")
            f1_att = ts["f1_att_by_aerial"]
            f1_prf = ts["f1_prf_by_aerial"]
            for a in AERIALS:
                att = f1_att[a]
                if att == 0:
                    continue
                rate = 100.0 * f1_prf[a] / att
                out(f"      {a:4s}        : {f1_prf[a]}/{att}  ({rate:.0f}%)")
        else:
            out(f"    Frame-1 aerials : no jump aerials detected")

        # Ledge tech
        lt = p.get("ledge_tech")
        if lt and lt.get("engagements", 0) > 0:
            eng = lt["engagements"]
            dwell_avg = lt["dwell_frames"] / lt["dwell_n"] if lt["dwell_n"] else 0.0
            if lt["hang_frames"] > 0:
                inv = f"{100.0 * lt['hang_invuln_frames'] / lt['hang_frames']:.0f}% invuln on ledge"
            else:
                inv = "invuln n/a"
            out(f"    Ledge tech      : {eng} grabs, avg {dwell_avg:.0f}f to act, {inv}")
            ld = lt["ledgedash_count"]
            if ld > 0:
                gpct = 100.0 * lt["galint_pos"] / lt["galint_n"] if lt["galint_n"] else 0.0
                head = (f"{ld} ledgedashes, GALINT avg {lt['galint_sum'] / lt['galint_n']:.0f}f"
                        f" best {lt['galint_max']}f ({gpct:.0f}% keep invuln)")
                react = lt["ld_reaction_sum"] / ld
                wland = lt["ld_waveland_sum"] / ld
                fall  = (lt["ld_fall_sum"] / lt["ld_fall_n"]) if lt["ld_fall_n"] else 0.0
                sub = f"reaction {react:.0f}f, fall {fall:.0f}f, waveland {wland:.0f}f"
                if lt["galint_n"] > 0:
                    sub += f", dist {lt['ld_distance_sum'] / lt['galint_n']:.1f}"
                out(f"      Ledgedash     : {head}  ({sub})")
            opt_str = ", ".join(
                f"{o} {lt['option_counts'][o]}"
                for o in LEDGE_OPTIONS if lt["option_counts"].get(o, 0) > 0
            )
            err = "  [!] ledge-jump = tech error" if lt["option_counts"].get("ledge_jump_direct", 0) > 0 else ""
            out(f"      Options       : {opt_str}{err}")
        else:
            out(f"    Ledge tech      : no ledge grabs detected")

        # Post-landing options
        pl = p.get("post_landing")
        if pl and pl.get("samples", 0) > 0:
            avg = pl["avg_frames_to_act"]
            out(f"    Post-landing    : {pl['samples']} samples, avg {avg:.1f}f to act")
            out(f"      {_format_postland_categories(pl['categories'], pl['samples'])}")
            for a in POSTLAND_AERIAL_BUCKETS:
                bucket = pl["by_aerial"].get(a, {})
                bs = bucket.get("samples", 0)
                if bs == 0:
                    continue
                bavg = bucket["total_wait_frames"] / bs
                cats_str = _format_postland_categories(bucket["categories"], bs)
                out(f"      {a:5s} ({bs:3d}) avg {bavg:4.1f}f  {cats_str}")

        # --- 2. Stage Control ---
        out()
        out("  [2] STAGE CONTROL")
        out()
        if sc["total_frames"] > 0:
            flag = _pct_flag(sc["center_pct"], lo=40, hi=60)
            out(f"    Center stage    : {sc['center_pct']:.1f}% of game time{flag}")
        else:
            out(f"    Center stage    : no data")

        # --- 3. Edgeguarding (opponent's recovery situations vs this player) ---
        out()
        out("  [3] EDGEGUARDING  (opponent's recovery attempts)")
        out()
        above = eg["above"]
        below = eg["below"]
        def eg_line(label, d):
            if d["attempts"] == 0:
                out(f"    {label:<18}: 0 attempts")
            else:
                conv_pct = pct(d["conversions"], d["attempts"])
                out(f"    {label:<18}: {d['conversions']}/{d['attempts']} converted"
                    f"  ({conv_pct:.0f}%)")
        eg_line("Above ledge", above)
        eg_line("Below ledge", below)
        total_eg = above["attempts"] + below["attempts"]
        total_conv = above["conversions"] + below["conversions"]
        if total_eg > 0:
            out(f"    {'Total':<18}: {total_conv}/{total_eg} converted"
                f"  ({pct(total_conv, total_eg):.0f}%)")

        # --- 4. Neutral ---
        out()
        out("  [4] NEUTRAL")
        out()
        out(f"    Shield time     : {nt['shield_seconds']:.1f}s")
        out(f"    Crouch time     : {nt['crouch_seconds']:.1f}s")
        ratio = nt["shield_frames"] + nt["crouch_frames"]
        if ratio > 0:
            shield_share = pct(nt["shield_frames"], ratio)
            out(f"    Shield vs crouch: {shield_share:.0f}% shield / "
                f"{100 - shield_share:.0f}% crouch  (of defensive frames)")

        # --- 5. Punish ---
        out()
        out("  [5] PUNISH")
        out()
        seqs = pu["sequences"]
        if seqs:
            out(f"    Sequences       : {pu['count']}")
            out(f"    Avg damage      : {pu['avg_damage']:.1f}%")
            out(f"    Outcomes        : "
                f"{pu['kills']} kills / "
                f"{pu['edgeguards']} edgeguards / "
                f"{pu['resets']} resets")
            out(f"    Largest punishes:")
            for s in sorted(seqs, key=lambda x: -x["damage"])[:5]:
                out(f"      [{s['time']}]  {s['damage']:.1f}%  "
                    f"({s.get('start_pct', 0):.0f}->{s.get('end_pct', 0):.0f}%)  "
                    f"~{s['hits']} hit(s)  -> {s['outcome']}")
        else:
            out(f"    No punish sequences detected")
        out()

    # Head-to-head snapshot (2-player only)
    if len(order) == 2 and focus_port is None:
        p0 = ports[order[0]]
        p1 = ports[order[1]]
        out("=" * 62)
        out("  HEAD-TO-HEAD SNAPSHOT")
        out("=" * 62)
        out()

        def row(label, v0, v1):
            out(f"    {label:<22}  {str(v0):<18}  {str(v1)}")

        row("", p0["label"], p1["label"])
        out("    " + "-" * 56)
        row("Stocks lost",
            p0["stocks_lost"], p1["stocks_lost"])
        row("Center stage %",
            f"{p0['stage_control']['center_pct']:.1f}%",
            f"{p1['stage_control']['center_pct']:.1f}%")
        row("L-cancel rate",
            f"{p0['tech_skill']['l_cancel_rate']:.0f}% ({p0['tech_skill']['l_cancel_success']}/{p0['tech_skill']['l_cancel_attempts']})",
            f"{p1['tech_skill']['l_cancel_rate']:.0f}% ({p1['tech_skill']['l_cancel_success']}/{p1['tech_skill']['l_cancel_attempts']})")
        row("Wavedash rate",
            f"{p0['tech_skill']['wd_rate']:.0f}% ({p0['tech_skill']['wd_perfect']}/{p0['tech_skill']['wd_attempts']})",
            f"{p1['tech_skill']['wd_rate']:.0f}% ({p1['tech_skill']['wd_perfect']}/{p1['tech_skill']['wd_attempts']})")
        row("Frame-1 aerial rate",
            f"{p0['tech_skill']['f1_rate']:.0f}% ({p0['tech_skill']['f1_perfect']}/{p0['tech_skill']['f1_attempts']})",
            f"{p1['tech_skill']['f1_rate']:.0f}% ({p1['tech_skill']['f1_perfect']}/{p1['tech_skill']['f1_attempts']})")
        row("Avg punish taken",
            f"{p0['punishes']['avg_damage']:.1f}%",
            f"{p1['punishes']['avg_damage']:.1f}%")
        row("Shield time",
            f"{p0['neutral']['shield_seconds']:.1f}s",
            f"{p1['neutral']['shield_seconds']:.1f}s")
        row("Crouch time",
            f"{p0['neutral']['crouch_seconds']:.1f}s",
            f"{p1['neutral']['crouch_seconds']:.1f}s")
        row("Edgeguard conversion",
            f"{p0['edgeguard']['above']['conversions'] + p0['edgeguard']['below']['conversions']}/"
            f"{p0['edgeguard']['above']['attempts'] + p0['edgeguard']['below']['attempts']}",
            f"{p1['edgeguard']['above']['conversions'] + p1['edgeguard']['below']['conversions']}/"
            f"{p1['edgeguard']['above']['attempts'] + p1['edgeguard']['below']['attempts']}")
        out()

    out("=" * 62)
    out("  END OF REPORT")
    out("=" * 62)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze(slp_path, focus_port=None, my_code=None):
    """Parse a .slp file. Returns (report_str, game_data_dict).

    If my_code is given and focus_port is None, auto-detect port from connect code.
    """
    try:
        analyzer = GameAnalyzer(slp_path)
    except Exception:
        return None, None
    if focus_port is None and my_code:
        focus_port = detect_port(slp_path, my_code)
    analyzer.run()
    game_data = analyzer.build_data()
    report    = format_report(game_data, focus_port=focus_port)
    return report, game_data


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Parse a Slippi .slp file for VOD review.")
    parser.add_argument("slp_file", help="Path to .slp replay file")
    parser.add_argument("--port", type=int,  default=None, help="Focus port (0-indexed)")
    parser.add_argument("--code", type=str,  default=None, help="Your Slippi connect code (e.g. ABCD#123) for auto port detection")
    parser.add_argument("--out",  type=str,  default=None, help="Write report to file")
    args = parser.parse_args()

    report, _ = analyze(args.slp_file, focus_port=args.port, my_code=args.code)
    if report is None:
        print("Failed to parse replay.")
        sys.exit(1)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Report saved to {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
