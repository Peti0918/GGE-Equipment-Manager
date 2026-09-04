"""Goodgame Empire commander equipment helper - interactive persistent session.

Start once:
    py main.py

After login the bot stays connected. Commands can be entered without logging in
again:
    help
    status
    save
    save force
    unequip
    restore
    quit

The program does not contain robber-baron/combat automation.
Snapshots are stored per player ID in commander_loadouts/player_<PID>.json.
"""

import asyncio
import json
import random
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

import websockets
from websockets.exceptions import ConnectionClosed

from protocol import encode_xt, decode_xt
from state import GameState

with open("err.json", "r", encoding="utf-8") as f:
    ERRORS = {int(float(k)): v for k, v in json.load(f).items()}

VER_CHK = '<msg t="sys"><body action="verChk" r="0"><ver v="166"/></body></msg>'
API_OK = "<msg t='sys'><body action='apiOK' r='0'></body></msg>"
JOIN_OK = "<msg t='sys'><body action='joinOK' r='1'><pid id='0'/><vars /><uLs r='1'></uLs></body></msg>"
ROUND_TRIP = '<msg t="sys"><body action="roundTrip" r="1"></body></msg>'
AUTO_JOIN = '<msg t="sys"><body action="autoJoin" r="-1"></body></msg>'

ERR_IS_BANNED = 27
ERR_RETRY = 453
KEEPALIVE_SECONDS = 60
INITIAL_STATE_SECONDS = 8
EQUIP_DELAY_SECONDS = 0.35
LOADOUT_DIR = Path("commander_loadouts")


def log(*parts):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}]", *parts)


def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        sys.exit("config.json not found.")

    required = ("username", "password", "game_url", "game_server")
    missing = [key for key in required if not config.get(key)]
    if missing:
        sys.exit(f"config.json: missing/empty field(s): {', '.join(missing)}")

    return config


def _as_text(raw):
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw


async def recv_xt(ws, timeout=15):
    """Wait for the next %xt% frame, skipping plain XML frames."""
    while True:
        raw = _as_text(await asyncio.wait_for(ws.recv(), timeout))
        if raw.startswith("%"):
            return decode_xt(raw)


async def wait_for_join_ok(ws, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for joinOK")

        raw = _as_text(await asyncio.wait_for(ws.recv(), remaining))
        if raw == JOIN_OK:
            return

        if raw.startswith("%"):
            cmd, _, _ = decode_xt(raw)
            if cmd == "rlu":
                log("Got rlu, sending autoJoin...")
                await ws.send(AUTO_JOIN)
            continue

        log(f"(while waiting for joinOK, got instead): {raw[:300]!r}")


async def recv_xml_until(ws, expected, label, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for {label}")
        raw = _as_text(await asyncio.wait_for(ws.recv(), remaining))
        if raw == expected:
            return
        log(f"(while waiting for {label}, got instead): {raw[:300]!r}")


async def login(ws, config):
    zone = config["game_server"]

    log("Sending verChk...")
    await ws.send(VER_CHK)
    await recv_xml_until(ws, API_OK, "apiOK")
    log("Got apiOK")

    login_xml = (
        f'<msg t="sys"><body action="login" r="0"><login z="{zone}">'
        "<nick><![CDATA[]]></nick><pword><![CDATA[undefined%en%0]]></pword>"
        "</login></body></msg>"
    )
    await ws.send(login_xml)
    await wait_for_join_ok(ws)
    log("Got joinOK")

    await ws.send(ROUND_TRIP)
    vck_payload = f"undefined%web-html5%<RoundHouseKick>%{random.random() * sys.float_info.max:.0f}"
    await ws.send(encode_xt(zone, "vck", vck_payload))
    log("Sent roundTrip + vck, waiting for vck ack...")

    while True:
        cmd, _, _ = await recv_xt(ws, timeout=15)
        if cmd == "vck":
            break
    log("Got vck ack")

    lli_payload = {
        "CONM": 212,
        "RTM": 25,
        "ID": 0,
        "PL": 1,
        "NOM": config["username"],
        "PW": config["password"],
        "LT": None,
        "LANG": "en",
        "DID": "0",
        "AID": "1745592024940879420",
        "KID": "",
        "REF": "https://empire.goodgamestudios.com",
        "GCI": "",
        "SID": 9,
        "PLFID": 1,
    }
    await ws.send(encode_xt(zone, "lli", lli_payload))
    log("Sent lli (credentials), waiting for response...")

    while True:
        cmd, result, obj = await recv_xt(ws, timeout=15)
        if cmd != "lli":
            continue

        if result == ERR_RETRY:
            cooldown = obj.get("CD", "?") if isinstance(obj, dict) else "?"
            log(f"Rate limited by the server, retry in {cooldown}s")
            return False
        if result == ERR_IS_BANNED:
            if isinstance(obj, dict) and isinstance(obj.get("RS"), (int, float)):
                log(f"Account is banned, retry in {obj['RS'] / 3600:.2f}h")
            else:
                log("Account is banned.")
            return False
        if result != 0:
            log(f"Login failed, result code {result}: {obj}")
            return False

        log(f"Logged in as {config['username']}")
        return True


class XTDispatcher:
    """Single-reader dispatcher.

    websockets does not allow multiple coroutines to call recv() concurrently.
    The background receiver is therefore the only reader. Command handlers
    register a waiter before sending an eeq request; the matching response is
    delivered here while every frame still updates GameState.
    """

    def __init__(self, state):
        self.state = state
        self.waiters = defaultdict(deque)
        self.closed = False
        self.close_exc = None

    def create_waiter(self, cmd_name):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.waiters[cmd_name].append(future)
        return future

    def discard_waiter(self, cmd_name, future):
        queue = self.waiters.get(cmd_name)
        if not queue:
            return
        try:
            queue.remove(future)
        except ValueError:
            pass

    def feed(self, cmd, result, obj):
        self.state.handle(cmd, result, obj)

        queue = self.waiters.get(cmd)
        if not queue:
            return

        while queue:
            future = queue.popleft()
            if future.done():
                continue
            future.set_result((result, obj))
            break

    def close(self, exc):
        self.closed = True
        self.close_exc = exc
        for queue in self.waiters.values():
            while queue:
                future = queue.popleft()
                if not future.done():
                    future.set_exception(ConnectionError("game connection closed"))


async def receiver_loop(ws, dispatcher):
    """Continuously drain server pushes for the whole logged-in session."""
    try:
        while True:
            raw = _as_text(await ws.recv())
            if not raw.startswith("%"):
                continue
            cmd, result, obj = decode_xt(raw)
            dispatcher.feed(cmd, result, obj)
    except (ConnectionClosed, ConnectionResetError) as exc:
        dispatcher.close(exc)
        log(f"Connection closed by the server ({exc!r}).")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        dispatcher.close(exc)
        log(f"Receiver stopped because of an unexpected error: {exc!r}")


async def keepalive(ws, zone):
    try:
        while True:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            await ws.send(encode_xt(zone, "pin", "<RoundHouseKick>"))
    except (ConnectionClosed, ConnectionResetError):
        pass
    except asyncio.CancelledError:
        raise


async def send_xt_and_wait(ws, dispatcher, zone, cmd_name, payload, timeout=15):
    """Send one XT command and wait for its matching response via dispatcher."""
    if dispatcher.closed:
        raise ConnectionError("game connection is already closed")

    waiter = dispatcher.create_waiter(cmd_name)
    try:
        await ws.send(encode_xt(zone, cmd_name, payload))
        return await asyncio.wait_for(waiter, timeout)
    except asyncio.TimeoutError as exc:
        dispatcher.discard_waiter(cmd_name, waiter)
        raise TimeoutError(f"timed out waiting for '{cmd_name}'") from exc
    except Exception:
        dispatcher.discard_waiter(cmd_name, waiter)
        raise


async def collect_initial_state(state, dispatcher, seconds=INITIAL_STATE_SECONDS):
    """Receiver is already active; give login-time pushes a window to arrive."""
    log(f"Listening for {seconds}s to collect commander/equipment data...")
    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        if dispatcher.closed:
            break
        await asyncio.sleep(0.1)

    if state.commanders:
        log(f"Found {len(state.commanders)} commander(s)")


def loadout_path(player_id):
    LOADOUT_DIR.mkdir(exist_ok=True)
    return LOADOUT_DIR / f"player_{player_id}.json"


def make_loadout_snapshot(state):
    """Save equipment by visible position while retaining stable LID identity."""
    commanders = []

    for commander in sorted(
        state.commanders,
        key=lambda c: (
            c.get("position") is None,
            c.get("position") if c.get("position") is not None else 10**9,
            c.get("lord_id", 10**9),
        ),
    ):
        lord_id = commander.get("lord_id")
        if lord_id is None:
            continue

        items = []
        if commander.get("equipment_known", False):
            for eq in commander.get("equipment", []):
                if not isinstance(eq, list) or len(eq) < 2:
                    continue
                items.append({"equipment_id": eq[0], "slot": eq[1]})

        commanders.append(
            {
                "position": commander.get("position"),
                "lord_id_at_save": lord_id,
                "name": commander.get("name") or "",
                "was_moving": lord_id in state.moving_lord_ids,
                "equipment_known": bool(commander.get("equipment_known", False)),
                "equipment": items,
                "sources": sorted(commander.get("sources", [])),
            }
        )

    return {
        "version": 2,
        "player_id": state.player_id,
        "created_at": int(time.time()),
        "mapping": "visible_position",
        "commanders": commanders,
    }


def save_snapshot(state, path):
    snapshot = make_loadout_snapshot(state)
    with path.open("w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    return snapshot


def read_snapshot(path, player_id):
    try:
        with path.open("r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except FileNotFoundError:
        log(f"No saved equipment snapshot exists yet: {path}")
        return None
    except (OSError, json.JSONDecodeError) as exc:
        log(f"Could not read equipment snapshot {path}: {exc}")
        return None

    if snapshot.get("player_id") != player_id:
        log("Snapshot belongs to a different account; refusing to use it.")
        return None
    return snapshot


def normalize_snapshot(snapshot):
    """Convert legacy v1 snapshot shape to the current v2 list shape."""
    version = snapshot.get("version", 1)
    if version == 2:
        return snapshot

    if version != 1:
        return None

    legacy = []
    for lord_id_text, commander in snapshot.get("commanders", {}).items():
        try:
            saved_lid = int(lord_id_text)
        except (TypeError, ValueError):
            continue
        legacy.append(
            {
                "position": commander.get("position"),
                "lord_id_at_save": saved_lid,
                "name": commander.get("name") or "",
                "was_moving": False,
                "equipment_known": True,
                "equipment": commander.get("equipment", []),
                "sources": ["legacy-v1"],
            }
        )

    normalized = dict(snapshot)
    normalized["version"] = 2
    normalized["commanders"] = legacy
    return normalized


def current_equipment_locations(state):
    result = {}
    for commander in state.commanders:
        lord_id = commander.get("lord_id")
        position = commander.get("position")
        for eq in commander.get("equipment", []):
            if isinstance(eq, list) and len(eq) >= 2:
                result[eq[0]] = (lord_id, position, eq[1])
    return result


def commander_slot_map(commander):
    result = {}
    if not commander:
        return result
    for eq in commander.get("equipment", []):
        if isinstance(eq, list) and len(eq) >= 2:
            result[eq[1]] = eq[0]
    return result


def add_equipment_to_state(commander, equipment_id, slot):
    """Keep local state useful between interactive commands in this same login."""
    if commander is None:
        return
    current = []
    for eq in commander.get("equipment", []):
        if not isinstance(eq, list) or len(eq) < 2:
            continue
        if eq[1] == slot or eq[0] == equipment_id:
            continue
        current.append(eq)
    # Only EID+slot are required by this helper; later server pushes may replace
    # this minimal row with the complete equipment structure.
    current.append([equipment_id, slot])
    commander["equipment"] = current
    commander["equipment_known"] = True


def remove_equipment_from_state(commander, equipment_id):
    if commander is None:
        return
    commander["equipment"] = [
        eq
        for eq in commander.get("equipment", [])
        if not (isinstance(eq, list) and eq and eq[0] == equipment_id)
    ]
    commander["equipment_known"] = True


async def restore_loadout(ws, dispatcher, zone, state, path):
    snapshot = read_snapshot(path, state.player_id)
    if snapshot is None:
        log("Restore requires a previous 'save'. Nothing was changed.")
        return

    snapshot = normalize_snapshot(snapshot)
    if snapshot is None:
        log("Unsupported snapshot version; refusing to restore.")
        return

    equipped_locations = current_equipment_locations(state)
    restored = already_ok = skipped_busy = skipped_missing = 0
    skipped_conflict = skipped_unknown = failed = 0

    saved_commanders = sorted(
        snapshot.get("commanders", []),
        key=lambda c: (
            c.get("position") is None,
            c.get("position") if c.get("position") is not None else 10**9,
        ),
    )

    for saved in saved_commanders:
        position = saved.get("position")
        items = saved.get("equipment", [])

        if position is None:
            skipped_missing += len(items)
            log(f"Saved LID {saved.get('lord_id_at_save')} had no known VIS position; skipping.")
            continue

        if not saved.get("equipment_known", False):
            skipped_unknown += 1
            log(f"Position {position + 1}: saved equipment was incomplete; skipping.")
            continue

        target = state.commander_at_position(position)
        if target is None:
            skipped_missing += len(items)
            log(f"Position {position + 1}: no current commander there; skipping.")
            continue

        target_lord_id = target.get("lord_id")
        if target_lord_id in state.moving_lord_ids or target.get("busy"):
            skipped_busy += len(items)
            log(f"Position {position + 1}: LID {target_lord_id} is moving; skipping.")
            continue

        saved_lid = saved.get("lord_id_at_save")
        if saved_lid != target_lord_id:
            log(
                f"Position {position + 1}: LID changed {saved_lid} -> {target_lord_id}; "
                "restoring by saved VIS position."
            )

        current_slots = commander_slot_map(target)

        for item in sorted(items, key=lambda x: x.get("slot", 999)):
            equipment_id = item.get("equipment_id")
            slot = item.get("slot")
            if equipment_id is None or slot is None:
                continue

            live_target = state.commander_at_position(position)
            if live_target is None or live_target.get("lord_id") != target_lord_id:
                skipped_missing += 1
                log(f"Position {position + 1} changed during restore; skipping EID {equipment_id}.")
                continue
            if target_lord_id in state.moving_lord_ids or live_target.get("busy"):
                skipped_busy += 1
                log(f"Position {position + 1} became busy; skipping EID {equipment_id}.")
                continue

            existing = current_slots.get(slot)
            if existing == equipment_id:
                already_ok += 1
                continue
            if existing is not None:
                skipped_conflict += 1
                log(
                    f"Position {position + 1}, slot {slot}: EID {existing} is already there; "
                    f"not overwriting it with {equipment_id}."
                )
                continue

            elsewhere = equipped_locations.get(equipment_id)
            if elsewhere is not None:
                other_lid, other_pos, other_slot = elsewhere
                skipped_conflict += 1
                pos_text = other_pos + 1 if isinstance(other_pos, int) else "?"
                log(
                    f"EID {equipment_id} is already worn at position {pos_text} "
                    f"(LID {other_lid}, slot {other_slot}); not moving it automatically."
                )
                continue

            log(
                f"Position {position + 1}, slot {slot}: "
                f"equipping EID {equipment_id} -> LID {target_lord_id}..."
            )

            try:
                result, obj = await send_xt_and_wait(
                    ws,
                    dispatcher,
                    zone,
                    "eeq",
                    {"EID": equipment_id, "LID": target_lord_id, "E": 1},
                    timeout=15,
                )
            except (TimeoutError, ConnectionError) as exc:
                failed += 1
                log(f"No usable eeq response for EID {equipment_id}: {exc}")
                await asyncio.sleep(EQUIP_DELAY_SECONDS)
                continue

            if result == 0:
                restored += 1
                current_slots[slot] = equipment_id
                equipped_locations[equipment_id] = (target_lord_id, position, slot)
                add_equipment_to_state(live_target, equipment_id, slot)
            else:
                failed += 1
                log(
                    f"Could not equip EID {equipment_id}: "
                    f"{ERRORS.get(result, result)} ({obj})"
                )

            await asyncio.sleep(EQUIP_DELAY_SECONDS)

    log(
        "Restore finished: "
        f"{restored} equipped, {already_ok} already correct, "
        f"{skipped_busy} busy, {skipped_missing} missing/changed, "
        f"{skipped_conflict} conflicts, {skipped_unknown} unknown, {failed} failed."
    )


async def unequip_saved_commanders(ws, dispatcher, zone, state, path):
    snapshot = read_snapshot(path, state.player_id)
    if snapshot is None:
        log("Unequip requires a previous 'save'. Nothing was changed.")
        return

    snapshot = normalize_snapshot(snapshot)
    if snapshot is None:
        log("Unsupported snapshot version; refusing to unequip.")
        return

    eligible_lids = set()
    for commander in snapshot.get("commanders", []):
        if not commander.get("equipment_known", False):
            continue
        if not commander.get("equipment"):
            continue
        try:
            eligible_lids.add(int(commander.get("lord_id_at_save")))
        except (TypeError, ValueError):
            continue

    if not eligible_lids:
        log("Snapshot contains no commanders with saved equipment; nothing to remove.")
        return

    removed = already_empty = skipped_busy = skipped_missing = skipped_unknown = failed = 0

    for lord_id in sorted(eligible_lids):
        commander = state.commander_by_id(lord_id)
        if commander is None:
            skipped_missing += 1
            log(f"Saved commander LID {lord_id} is not currently available; skipping.")
            continue

        position = commander.get("position")
        pos_text = position + 1 if isinstance(position, int) else "?"

        if lord_id in state.moving_lord_ids or commander.get("busy"):
            skipped_busy += 1
            log(f"LID {lord_id} (position {pos_text}) is moving; skipping.")
            continue

        if not commander.get("equipment_known", False):
            skipped_unknown += 1
            log(f"LID {lord_id}: current equipment data is unknown; skipping.")
            continue

        current_items = [
            eq
            for eq in commander.get("equipment", [])
            if isinstance(eq, list) and len(eq) >= 2
        ]

        if not current_items:
            already_empty += 1
            log(f"LID {lord_id}: already has no equipment.")
            continue

        log(
            f"LID {lord_id} (position {pos_text}): "
            f"removing {len(current_items)} item(s)..."
        )

        for eq in list(current_items):
            equipment_id, slot = eq[0], eq[1]
            live = state.commander_by_id(lord_id)
            if live is None:
                skipped_missing += 1
                log(f"LID {lord_id} disappeared; stopping this commander.")
                break
            if lord_id in state.moving_lord_ids or live.get("busy"):
                skipped_busy += 1
                log(f"LID {lord_id} became busy; stopping this commander.")
                break

            log(f"LID {lord_id}, slot {slot}: removing EID {equipment_id}...")

            try:
                result, obj = await send_xt_and_wait(
                    ws,
                    dispatcher,
                    zone,
                    "eeq",
                    {"EID": equipment_id, "LID": lord_id, "E": 0},
                    timeout=15,
                )
            except (TimeoutError, ConnectionError) as exc:
                failed += 1
                log(f"No usable eeq response while removing EID {equipment_id}: {exc}")
                await asyncio.sleep(EQUIP_DELAY_SECONDS)
                continue

            if result == 0:
                removed += 1
                remove_equipment_from_state(live, equipment_id)
            else:
                failed += 1
                log(
                    f"Could not remove EID {equipment_id} from LID {lord_id}: "
                    f"{ERRORS.get(result, result)} ({obj})"
                )

            await asyncio.sleep(EQUIP_DELAY_SECONDS)

    log(
        "Unequip finished: "
        f"{removed} removed, {already_empty} already-empty commanders, "
        f"{skipped_busy} busy, {skipped_missing} missing, "
        f"{skipped_unknown} unknown, {failed} failed."
    )


def print_status(state, snapshot_path):
    moving_count = len(state.moving_lord_ids)
    unknown_count = sum(
        1 for commander in state.commanders if not commander.get("equipment_known", False)
    )
    equipped_count = sum(
        len(
            [
                eq
                for eq in commander.get("equipment", [])
                if isinstance(eq, list) and len(eq) >= 2
            ]
        )
        for commander in state.commanders
    )

    log(f"Player ID: {state.player_id}")
    log(
        f"Current state: {len(state.commanders)} commanders, "
        f"{equipped_count} equipped items, {moving_count} moving, "
        f"{unknown_count} with unknown equipment data."
    )
    if snapshot_path.exists():
        log(f"Saved snapshot: {snapshot_path}")
    else:
        log("Saved snapshot: NONE")


def print_help():
    print(
        "\nCommands:\n"
        "  status       show current commander/equipment state\n"
        "  save         save a snapshot (will NOT overwrite an existing one)\n"
        "  save force   intentionally overwrite the existing snapshot\n"
        "  unequip      remove gear from commanders that had gear in the snapshot\n"
        "  restore      restore the snapshot to the saved visible positions\n"
        "  help         show this help\n"
        "  quit / exit  close the bot and log out\n"
    )


async def read_console_command():
    """Read console input without blocking keepalive/server receiver tasks."""
    return await asyncio.to_thread(input, "gge> ")


async def interactive_console(ws, dispatcher, zone, state):
    snapshot_path = loadout_path(state.player_id)

    print_help()
    log("Connected and idle. No equipment action is automatic.")

    while not dispatcher.closed:
        try:
            line = (await read_console_command()).strip()
        except EOFError:
            line = "quit"

        if not line:
            continue

        parts = line.lower().split()
        command = parts[0]

        if command in ("quit", "exit", "q"):
            log("Closing connection by user request.")
            return

        if command in ("help", "?", "h"):
            print_help()
            continue

        if command == "status":
            print_status(state, snapshot_path)
            continue

        if command == "save":
            force = len(parts) >= 2 and parts[1] in ("force", "--force", "!")
            if len(parts) >= 2 and not force:
                log("Usage: save  OR  save force")
                continue

            if snapshot_path.exists() and not force:
                log(
                    f"Snapshot already exists: {snapshot_path}. "
                    "Nothing was overwritten. Use 'save force' only intentionally."
                )
                continue

            snapshot = save_snapshot(state, snapshot_path)
            total_items = sum(
                len(commander.get("equipment", []))
                for commander in snapshot["commanders"]
            )
            moving_saved = sum(
                1 for commander in snapshot["commanders"] if commander.get("was_moving")
            )
            unknown_saved = sum(
                1
                for commander in snapshot["commanders"]
                if not commander.get("equipment_known", False)
            )
            log(
                f"SNAPSHOT SAVED: {snapshot_path} "
                f"({len(snapshot['commanders'])} commanders, {total_items} items, "
                f"{moving_saved} moving, {unknown_saved} equipment-unknown)"
            )
            if unknown_saved:
                log(
                    "WARNING: some commanders had unknown equipment data. "
                    "Those entries will be skipped during restore."
                )
            continue

        if command == "unequip":
            await unequip_saved_commanders(
                ws, dispatcher, zone, state, snapshot_path
            )
            continue

        if command == "restore":
            await restore_loadout(ws, dispatcher, zone, state, snapshot_path)
            continue

        log(f"Unknown command: {line!r}. Type 'help'.")

    log("The game connection is no longer active; command console is stopping.")


async def run():
    config = load_config()
    zone = config["game_server"]

    state = GameState()
    state.zone = zone

    async with websockets.connect(f"wss://{config['game_url']}/") as ws:
        if not await login(ws, config):
            return

        dispatcher = XTDispatcher(state)
        receiver_task = asyncio.create_task(receiver_loop(ws, dispatcher))
        keepalive_task = asyncio.create_task(keepalive(ws, zone))

        try:
            await collect_initial_state(state, dispatcher)

            if dispatcher.closed:
                log("Connection closed during initial state collection.")
                return
            if state.player_id is None:
                log("Could not determine player ID. No action was performed.")
                return
            if not state.commanders:
                log("No commanders found. No action was performed.")
                return

            print_status(state, loadout_path(state.player_id))
            await interactive_console(ws, dispatcher, zone, state)

        finally:
            receiver_task.cancel()
            keepalive_task.cancel()
            await asyncio.gather(receiver_task, keepalive_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print()
        log("Stopped by user.")
