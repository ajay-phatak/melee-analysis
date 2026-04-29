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
    python game_review.py path/to/game.slp --code WAWI#755
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

FPS = 60

# ---------------------------------------------------------------------------
# Action state sets
# ---------------------------------------------------------------------------

DAMAGE_STATES = set(range(75, 100)) | {38}

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
}
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
# Victim is knocked down (tech situation)
DOWN_STATES = {
    ActionState.DOWN_BOUND_U, ActionState.DOWN_WAIT_U,
    ActionState.DOWN_BOUND_D, ActionState.DOWN_WAIT_D,
    ActionState.SHIELD_BREAK_DOWN_U, ActionState.SHIELD_BREAK_DOWN_D,
}
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
LANDING_STATES = frozenset({
    ActionState.LANDING, ActionState.LANDING_FALL_SPECIAL,
    ActionState.LANDING_AIR_N, ActionState.LANDING_AIR_F,
    ActionState.LANDING_AIR_B, ActionState.LANDING_AIR_HI, ActionState.LANDING_AIR_LW,
})

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

DIRECT_CODES_PATH = (
    r"C:\Users\wizar\AppData\Roaming\Slippi Launcher"
    r"\netplay\User\Slippi\direct-codes.json"
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
    name = my_code.split("#")[0]  # "WAWI" from "WAWI#755"
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
    __slots__ = ["state", "x", "y", "airborne", "stocks", "damage", "l_cancel", "jumps"]

    def __init__(self, pre, post):
        self.state    = post.state
        self.x        = post.position.x
        self.y        = post.position.y
        self.airborne = post.airborne
        self.stocks   = post.stocks
        self.damage   = post.damage
        self.l_cancel = post.l_cancel  # 1=success, 2=miss, None=not landing
        self.jumps    = post.jumps     # jumps remaining


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
            if curr.l_cancel == 1:
                self.l_cancel_attempts += 1
                self.l_cancel_success  += 1
            elif curr.l_cancel == 2:
                self.l_cancel_attempts += 1

        # --- High aerials (autocancel) ---
        if prev in ATTACK_AIR_STATES and state == ActionState.LANDING:
            self.high_aerials += 1

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
            if self._frames_airborne <= 2:  # frame 2 = input on first airborne frame (1-frame state lag)
                self.f1_perfect += 1

        self._prev_state    = state
        self._prev_airborne = air


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

    def _is_offstage(self, pf):
        if pf.state in CLIFF_STATES:
            return True
        return abs(pf.x) > self.ledge_x and pf.airborne

    def _is_safe_on_stage(self, pf):
        return (
            not pf.airborne
            and abs(pf.x) <= self.ledge_x + 5
            and pf.state not in CLIFF_STATES
            and pf.state.value not in DAMAGE_STATES
            and pf.stocks > 0
        )

    def feed(self, frame_idx, curr, prev):
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
        else:
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
            "frame":     self._start,
            "category":  category,
            "converted": converted,
        })
        self._active = False

    def finalize(self):
        if self._active:
            self._close(converted=False)

    def summary(self):
        result = {
            "above": {"attempts": 0, "conversions": 0},
            "below": {"attempts": 0, "conversions": 0},
        }
        for s in self.situations:
            cat = s["category"]
            if cat in result:
                result[cat]["attempts"] += 1
                if s["converted"]:
                    result[cat]["conversions"] += 1
        return result


# ---------------------------------------------------------------------------
# 3b. State History + Neutral Event Classifier
# ---------------------------------------------------------------------------

NEUTRAL_LOOKBACK = 20  # frames to look back when classifying neutral events
ATTACK_LOOKBACK  = 8   # shorter window for identifying attacker's own recent action

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
        self._last_in_dmg    = False
        self._opener         = None   # "grab", "knockdown", "launch", or None
        self._prev_state     = None   # victim state from previous frame
        self._loser_context  = "unknown"
        self._winner_context = "unknown"
        self._is_continuation = False

    def feed(self, frame_idx, victim, victim_hist=None, attacker_hist=None):
        state  = victim.state
        in_dmg = state.value in DAMAGE_STATES

        if in_dmg:
            if frame_idx - self._last_dmg_frame > 60:
                if self._active:
                    self._close(frame_idx, victim, killed=False)

                # Classify opener from victim's pre-damage state
                prev = self._prev_state
                if prev in CAPTURE_STATES or prev in THROWN_STATES:
                    opener = "grab"
                elif prev in DOWN_STATES:
                    opener = "knockdown"
                elif state in DAMAGE_FLY_STATES and (prev is None or prev.value not in DAMAGE_STATES):
                    opener = "launch"
                else:
                    opener = None

                loser_ctx, winner_ctx, is_cont = _classify_neutral_event(
                    opener, victim_hist, attacker_hist
                )

                self._active          = True
                self._opener          = opener
                self._loser_context   = loser_ctx
                self._winner_context  = winner_ctx
                self._is_continuation = is_cont
                self._start_pct       = victim.damage
                self._start_frame     = frame_idx
                self._hits            = 1
                self._peak_pct        = victim.damage
            elif not self._last_in_dmg:
                self._hits += 1
            self._last_dmg_frame = frame_idx
            self._peak_pct = max(self._peak_pct, victim.damage)
        else:
            if self._active and frame_idx - self._last_dmg_frame > 90:
                self._close(frame_idx, victim, killed=False)

        self._last_in_dmg = in_dmg
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
                "hits":           self._hits,
                "opener":         self._opener,
                "outcome":        outcome,
                "loser_context":  self._loser_context,
                "winner_context": self._winner_context,
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
        self.stgctrl = {i: StageControlTracker(sd["center_x"])      for i in self.port_indices}
        self.neutral = {i: NeutralTracker()                         for i in self.port_indices}
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

        self._prev = {i: None for i in self.port_indices}
        self._last_pf = {i: None for i in self.port_indices}

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

                # Death detection (drives edgeguard + punish death notifications)
                died = self.deaths[port_idx].feed(frame_idx, curr)
                if died:
                    self.edgeguards[port_idx].notify_death()
                    self.punishes[port_idx].notify_death(frame_idx)

                if curr.stocks == 0:
                    self._prev[port_idx] = curr
                    self._last_pf[port_idx] = curr
                    continue

                self.tech[port_idx].feed(curr)
                self.stgctrl[port_idx].feed(curr)
                self.neutral[port_idx].feed(curr)
                opp_idx = self.opponent.get(port_idx, port_idx)
                self.punishes[port_idx].feed(
                    frame_idx, curr,
                    victim_hist=self.state_hist[port_idx],
                    attacker_hist=self.state_hist.get(opp_idx),
                )
                self.edgeguards[port_idx].feed(frame_idx, curr, prev)
                # push state AFTER trackers have consumed it so history lags current frame
                self.state_hist[port_idx].push(curr.state)

                self._prev[port_idx] = curr
                self._last_pf[port_idx] = curr

        last_frame = len(frames) - 1
        for port_idx in self.port_indices:
            last_pf = self._last_pf[port_idx]
            self.punishes[port_idx].finalize(last_frame, last_pf)
            self.edgeguards[port_idx].finalize()

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
                },
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
        else:
            out(f"    Frame-1 aerials : no jump aerials detected")

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
    parser.add_argument("--code", type=str,  default=None, help="Your Slippi connect code (e.g. WAWI#755) for auto port detection")
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
