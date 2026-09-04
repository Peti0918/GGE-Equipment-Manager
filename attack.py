"""Robber baron troop math and %xt% "cra" attack payload, reimplementing the
formulas from the reference bot's plugins/attack/attack.js and
plugins/attack/sharedBarronAttackLogic.js. Deliberately simplified for v1:
one wave, middle flank only, no tools, no reinforcement wave, no commander
skill bonuses.
"""

import json
import math
import time

with open("horses.json", "r", encoding="utf-8") as f:
    HORSES_BY_WOD_ID = {h["wodID"]: h for h in json.load(f)}

AREA_TYPE_BARRON = 2

# offset per kingdomID, matches sharedBarronAttackLogic.js's getLevel()
LEVEL_OFFSET = {0: 1, 1: 35, 2: 20, 3: 45}

WAVE_SLOT_THRESHOLDS = {
    "LT": [0, 37],
    "LU": [0, 13],
    "MT": [0, 11, 37],
    "MU": [0, 0, 13, 13, 26, 26],
    "RT": [0, 37],
    "RU": [0, 13],
}


def robber_baron_level(victories, kingdom_id):
    return int(1.9 * victories ** 0.555) + LEVEL_OFFSET.get(kingdom_id, 0)


def get_max_attackers(level):
    if level <= 69:
        return min(260, 5 * level + 8)
    return 320


def get_amount_soldiers_flank(level):
    return math.ceil(0.2 * get_max_attackers(level))


def get_amount_soldiers_front(level):
    return math.ceil(get_max_attackers(level) - 2 * get_amount_soldiers_flank(level))


def _slot_count(thresholds, level):
    return sum(1 for t in thresholds if t <= level)


def _empty_slots(thresholds, level):
    return [[-1, 0] for _ in range(_slot_count(thresholds, level))]


def assign_unit(troops, max_units):
    """Pops from the front of `troops` (list of {wod_id, amount}), same as attack.js's assignUnit."""
    if not troops:
        return -1, 0

    unit = troops[0]
    amount = max(0, min(unit["amount"], max_units))
    unit["amount"] -= amount
    if unit["amount"] <= 0:
        troops.pop(0)

    return (unit["wod_id"], amount) if amount > 0 else (-1, 0)


def is_burning(area):
    now_ms = int(time.time() * 1000)
    return (area["time_since_request"] + area["extra_data"][2] * 1000) > now_ms


def best_coin_horse(unlocked_horses):
    """Fastest coin-only (no rubies) stable horse the castle has unlocked -
    same selection as getAttackInfo() in the reference bot's attack.js.
    Without this, HBW stays -1 and every army travels at the slowest base
    speed, which is what made round trips (and commander wait times) so
    long before this."""
    best_wod_id, best_boost = -1, math.inf
    for wod_id in unlocked_horses:
        horse = HORSES_BY_WOD_ID.get(wod_id)
        if not horse:
            continue
        if float(horse["costFactorC1"]) <= 0 or float(horse["costFactorC2"]) != 0:
            continue
        boost = float(horse["unitBoost"])
        if boost < best_boost:
            best_wod_id, best_boost = wod_id, boost
    return best_wod_id


def build_middle_wave_attack(castle, target, lord_id, troops):
    """troops: list of {wod_id, amount} dicts, will be mutated (consumed) like the reference bot does."""
    level = robber_baron_level(target["extra_data"][1], target["kingdom_id"])

    m_u = _empty_slots(WAVE_SLOT_THRESHOLDS["MU"], level)
    max_troops = get_amount_soldiers_front(level)
    sent = 0
    for slot in m_u:
        wod_id, amount = assign_unit(troops, max_troops)
        if amount > 0:
            slot[0], slot[1] = wod_id, amount
            max_troops -= amount
            sent += amount

    wave = {
        "L": {"T": _empty_slots(WAVE_SLOT_THRESHOLDS["LT"], level), "U": _empty_slots(WAVE_SLOT_THRESHOLDS["LU"], level)},
        "R": {"T": _empty_slots(WAVE_SLOT_THRESHOLDS["RT"], level), "U": _empty_slots(WAVE_SLOT_THRESHOLDS["RU"], level)},
        "M": {"T": _empty_slots(WAVE_SLOT_THRESHOLDS["MT"], level), "U": m_u},
    }

    horse_wod_id = best_coin_horse(castle.get("unlocked_horses", []))

    payload = {
        "SX": castle["x"], "SY": castle["y"],
        "TX": target["x"], "TY": target["y"],
        "KID": target["kingdom_id"],
        "LID": lord_id,
        "WT": 0, "HBW": horse_wod_id, "BPC": 0, "ATT": 0, "AV": 0, "LP": 0, "FC": 0,
        "PTT": 0, "SD": 0, "ICA": 0, "CD": 99,
        "A": [wave],
        "BKS": [],
        "AST": [-1, -1, -1],
        "RW": [[-1, 0] for _ in range(8)],
        "ASCT": 0,
    }
    return payload, level, sent, horse_wod_id
