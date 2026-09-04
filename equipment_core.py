"""Persistent-session core for the Goodgame Empire commander equipment helper.

The module is UI-agnostic: Tkinter (or a future .exe GUI) can call the same
connect/save/unequip/restore methods without reconnecting between actions.
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


def load_errors(path="err.json"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {int(float(k)): v for k, v in json.load(f).items()}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


ERRORS = load_errors()


def _as_text(raw):
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw


async def recv_xt(ws, timeout=15):
    while True:
        raw = _as_text(await asyncio.wait_for(ws.recv(), timeout))
        if raw.startswith("%"):
            return decode_xt(raw)


class XTDispatcher:
    def __init__(self, state):
        self.state = state
        self.waiters = defaultdict(deque)
        self.closed = False
        self.close_exc = None

    def create_waiter(self, cmd_name):
        future = asyncio.get_running_loop().create_future()
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


class EquipmentBotSession:
    def __init__(self, log_callback=None, progress_callback=None):
        self.log_callback = log_callback or print
        self.progress_callback = progress_callback
        self.state = GameState()
        self.config = None
        self.zone = None
        self.ws = None
        self.dispatcher = None
        self.receiver_task = None
        self.keepalive_task = None
        self.connected = False
        self._connect_context = None
        self._action_lock = asyncio.Lock()

    def log(self, *parts):
        ts = time.strftime("%H:%M:%S")
        self.log_callback(f"[{ts}] " + " ".join(str(p) for p in parts))

    def progress(self, mode=None, current=0, total=0, active=False, message=""):
        if not self.progress_callback:
            return
        self.progress_callback({
            "mode": mode,
            "current": int(current),
            "total": int(total),
            "active": bool(active),
            "message": message,
        })

    async def _wait_for_join_ok(self, timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for joinOK")
            raw = _as_text(await asyncio.wait_for(self.ws.recv(), remaining))
            if raw == JOIN_OK:
                return
            if raw.startswith("%"):
                cmd, _, _ = decode_xt(raw)
                if cmd == "rlu":
                    self.log("Lobby response received; sending autoJoin...")
                    await self.ws.send(AUTO_JOIN)

    async def _recv_xml_until(self, expected, label, timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for {label}")
            raw = _as_text(await asyncio.wait_for(self.ws.recv(), remaining))
            if raw == expected:
                return

    async def _login(self):
        zone = self.zone
        self.log("Connecting to the game server...")
        await self.ws.send(VER_CHK)
        await self._recv_xml_until(API_OK, "apiOK")

        login_xml = (
            f'<msg t="sys"><body action="login" r="0"><login z="{zone}">'
            "<nick><![CDATA[]]></nick><pword><![CDATA[undefined%en%0]]></pword>"
            "</login></body></msg>"
        )
        await self.ws.send(login_xml)
        await self._wait_for_join_ok()

        await self.ws.send(ROUND_TRIP)
        vck_payload = f"undefined%web-html5%<RoundHouseKick>%{random.random() * sys.float_info.max:.0f}"
        await self.ws.send(encode_xt(zone, "vck", vck_payload))

        while True:
            cmd, _, _ = await recv_xt(self.ws, timeout=15)
            if cmd == "vck":
                break

        lli_payload = {
            "CONM": 212,
            "RTM": 25,
            "ID": 0,
            "PL": 1,
            "NOM": self.config["username"],
            "PW": self.config["password"],
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
        await self.ws.send(encode_xt(zone, "lli", lli_payload))

        while True:
            cmd, result, obj = await recv_xt(self.ws, timeout=15)
            if cmd != "lli":
                continue
            if result == ERR_RETRY:
                cooldown = obj.get("CD", "?") if isinstance(obj, dict) else "?"
                raise RuntimeError(f"The server temporarily rate-limited this login. Retry in: {cooldown} s")
            if result == ERR_IS_BANNED:
                raise RuntimeError("The server reports that this account is currently banned.")
            if result != 0:
                raise RuntimeError(f"Login failed ({result}): {obj}")
            return

    async def _receiver_loop(self):
        try:
            while True:
                raw = _as_text(await self.ws.recv())
                if not raw.startswith("%"):
                    continue
                cmd, result, obj = decode_xt(raw)
                self.dispatcher.feed(cmd, result, obj)
        except (ConnectionClosed, ConnectionResetError) as exc:
            if self.dispatcher:
                self.dispatcher.close(exc)
            self.connected = False
            self.log("The server closed the connection.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.dispatcher:
                self.dispatcher.close(exc)
            self.connected = False
            self.log(f"Receiver loop error: {exc!r}")

    async def _keepalive(self):
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_SECONDS)
                await self.ws.send(encode_xt(self.zone, "pin", "<RoundHouseKick>"))
        except (ConnectionClosed, ConnectionResetError):
            pass
        except asyncio.CancelledError:
            raise

    async def connect(self, config):
        if self.connected:
            return self.status()

        required = ("username", "password", "game_url", "game_server")
        missing = [key for key in required if not str(config.get(key, "")).strip()]
        if missing:
            raise ValueError("Missing field(s): " + ", ".join(missing))

        self.config = dict(config)
        self.zone = self.config["game_server"]
        self.state = GameState()

        try:
            self._connect_context = websockets.connect(f"wss://{self.config['game_url']}/")
            self.ws = await self._connect_context.__aenter__()
            await self._login()

            self.dispatcher = XTDispatcher(self.state)
            self.receiver_task = asyncio.create_task(self._receiver_loop())
            self.keepalive_task = asyncio.create_task(self._keepalive())

            self.log("Login successful. Loading account state...")
            deadline = time.monotonic() + INITIAL_STATE_SECONDS
            while time.monotonic() < deadline:
                if self.dispatcher.closed:
                    break
                await asyncio.sleep(0.1)

            if self.dispatcher.closed:
                raise ConnectionError("The connection was lost while loading the initial account state.")
            if self.state.player_id is None:
                raise RuntimeError("Could not determine the player ID.")
            if not self.state.commanders:
                raise RuntimeError("No commander list was received from the server.")

            self.connected = True

            # The login bundle normally contains esl (storage limits), while
            # gei gives us the exact current equipment inventory. Refresh gei
            # once so the UI can immediately show an accurate used/total count.
            try:
                await self.refresh_inventory(silent=True)
            except Exception as exc:
                self.log(f"[WARNING] Equipment storage usage could not be refreshed: {exc}")

            self.log("Connected. No equipment action will run automatically.")
            storage = self.equipment_storage_status()
            if storage.get("capacity") is not None:
                self.log(
                    f"Equipment storage: {storage.get('used', '?')}/{storage['capacity']} used, "
                    f"{storage.get('free', '?')} free."
                )
            return self.status()
        except Exception:
            await self.disconnect(silent=True)
            raise

    async def disconnect(self, silent=False):
        self.connected = False

        tasks = [t for t in (self.receiver_task, self.keepalive_task) if t is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self.receiver_task = None
        self.keepalive_task = None

        if self._connect_context is not None:
            try:
                await self._connect_context.__aexit__(None, None, None)
            except Exception:
                pass
        elif self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass

        self._connect_context = None
        self.ws = None
        self.dispatcher = None
        if not silent:
            self.log("Disconnected.")

    def _require_connected(self):
        if not self.connected or self.ws is None or self.dispatcher is None:
            raise RuntimeError("There is no active connection.")
        if self.dispatcher.closed:
            self.connected = False
            raise RuntimeError("The game connection has already been lost.")

    def loadout_path(self):
        if self.state.player_id is None:
            raise RuntimeError("Player ID is not available.")
        LOADOUT_DIR.mkdir(exist_ok=True)
        return LOADOUT_DIR / f"player_{self.state.player_id}.json"

    @staticmethod
    def _eligible_lids_from_snapshot(snapshot):
        eligible = set()
        if not isinstance(snapshot, dict):
            return eligible
        for commander in snapshot.get("commanders", []):
            if not commander.get("equipment_known", False) or not commander.get("equipment"):
                continue
            try:
                eligible.add(int(commander.get("lord_id_at_save")))
            except (TypeError, ValueError):
                pass
        return eligible

    def _snapshot_if_available(self):
        if self.state.player_id is None:
            return None
        path = LOADOUT_DIR / f"player_{self.state.player_id}.json"
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            if raw.get("player_id") != self.state.player_id:
                return None
            return self._normalize_snapshot(raw)
        except (OSError, json.JSONDecodeError, RuntimeError):
            return None

    def _count_current_unequip_candidates(self, snapshot=None):
        snapshot = snapshot if snapshot is not None else self._snapshot_if_available()
        eligible_lids = self._eligible_lids_from_snapshot(snapshot)
        count = 0
        for lord_id in eligible_lids:
            commander = self.state.commander_by_id(lord_id)
            if commander is None:
                continue
            if lord_id in self.state.moving_lord_ids or commander.get("busy"):
                continue
            if not commander.get("equipment_known", False):
                continue
            count += sum(
                1 for eq in commander.get("equipment", [])
                if isinstance(eq, list) and len(eq) >= 2
            )
        return count

    def equipment_storage_status(self, snapshot=None):
        capacity = self.state.inventory_capacity
        used = self.state.inventory_used
        free = self.state.inventory_free

        if capacity is not None and used is not None:
            free = max(0, capacity - used)
        elif capacity is not None and free is not None and used is None:
            used = max(0, capacity - free)

        required = self._count_current_unequip_candidates(snapshot)
        can_unequip = None
        remaining = None
        if free is not None:
            can_unequip = free >= required
            remaining = free - required

        return {
            "used": used,
            "capacity": capacity,
            "free": free,
            "required": required,
            "can_unequip": can_unequip,
            "remaining_after_unequip": remaining,
        }

    def status(self):
        equipped_count = sum(
            len([eq for eq in c.get("equipment", []) if isinstance(eq, list) and len(eq) >= 2])
            for c in self.state.commanders
        )
        unknown_count = sum(1 for c in self.state.commanders if not c.get("equipment_known", False))
        path = None
        snapshot_exists = False
        if self.state.player_id is not None:
            path = LOADOUT_DIR / f"player_{self.state.player_id}.json"
            snapshot_exists = path.exists()

        storage = self.equipment_storage_status()
        return {
            "connected": bool(self.connected),
            "player_id": self.state.player_id,
            "commanders": len(self.state.commanders),
            "equipped_items": equipped_count,
            "moving": len(self.state.moving_lord_ids),
            "unknown_equipment": unknown_count,
            "snapshot_exists": snapshot_exists,
            "snapshot_path": str(path) if path else "",
            "storage_used": storage["used"],
            "storage_capacity": storage["capacity"],
            "storage_free": storage["free"],
            "unequip_required": storage["required"],
            "can_unequip": storage["can_unequip"],
            "remaining_after_unequip": storage["remaining_after_unequip"],
        }

    def _make_snapshot(self):
        commanders = []
        for commander in sorted(
            self.state.commanders,
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
                    if isinstance(eq, list) and len(eq) >= 2:
                        items.append({"equipment_id": eq[0], "slot": eq[1]})
            commanders.append(
                {
                    "position": commander.get("position"),
                    "lord_id_at_save": lord_id,
                    "name": commander.get("name") or "",
                    "was_moving": lord_id in self.state.moving_lord_ids,
                    "equipment_known": bool(commander.get("equipment_known", False)),
                    "equipment": items,
                    "sources": sorted(commander.get("sources", [])),
                }
            )
        return {
            "version": 2,
            "player_id": self.state.player_id,
            "created_at": int(time.time()),
            "mapping": "visible_position",
            "commanders": commanders,
        }

    async def save_snapshot(self, force=False):
        self._require_connected()
        async with self._action_lock:
            path = self.loadout_path()
            if path.exists() and not force:
                return {"ok": False, "exists": True, "path": str(path)}
            snapshot = self._make_snapshot()
            with path.open("w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            total_items = sum(len(c.get("equipment", [])) for c in snapshot["commanders"])
            moving_saved = sum(1 for c in snapshot["commanders"] if c.get("was_moving"))
            unknown_saved = sum(1 for c in snapshot["commanders"] if not c.get("equipment_known", False))
            self.log(
                f"Snapshot saved: {len(snapshot['commanders'])} commanders, "
                f"{total_items} items, {moving_saved} moving, {unknown_saved} with unknown equipment data."
            )
            return {
                "ok": True,
                "path": str(path),
                "commanders": len(snapshot["commanders"]),
                "items": total_items,
                "moving": moving_saved,
                "unknown": unknown_saved,
            }

    def _read_snapshot(self):
        path = self.loadout_path()
        try:
            with path.open("r", encoding="utf-8") as f:
                snapshot = json.load(f)
        except FileNotFoundError:
            raise RuntimeError("There is no equipment snapshot for this account yet.")
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"The snapshot could not be read: {exc}") from exc
        if snapshot.get("player_id") != self.state.player_id:
            raise RuntimeError("The snapshot belongs to a different account.")
        return self._normalize_snapshot(snapshot)

    @staticmethod
    def _normalize_snapshot(snapshot):
        version = snapshot.get("version", 1)
        if version == 2:
            return snapshot
        if version != 1:
            raise RuntimeError(f"Unsupported snapshot version: {version}")
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

    async def _send_xt_and_wait(self, cmd_name, payload, timeout=15):
        self._require_connected()
        waiter = self.dispatcher.create_waiter(cmd_name)
        try:
            await self.ws.send(encode_xt(self.zone, cmd_name, payload))
            return await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError as exc:
            self.dispatcher.discard_waiter(cmd_name, waiter)
            raise TimeoutError(f"No response was received for the {cmd_name} command.") from exc
        except Exception:
            self.dispatcher.discard_waiter(cmd_name, waiter)
            raise

    async def refresh_inventory(self, silent=False):
        """Ask the server for the current equipment inventory (gei)."""
        self._require_connected()
        result, obj = await self._send_xt_and_wait("gei", {}, timeout=20)
        if result != 0:
            raise RuntimeError(f"gei failed: {ERRORS.get(result, result)} ({obj})")
        if not silent:
            storage = self.equipment_storage_status()
            self.log(
                f"Equipment storage refreshed: {storage.get('used', '?')}/"
                f"{storage.get('capacity', '?')} used, {storage.get('free', '?')} free."
            )
        return self.equipment_storage_status()

    @staticmethod
    def _slot_map(commander):
        result = {}
        if not commander:
            return result
        for eq in commander.get("equipment", []):
            if isinstance(eq, list) and len(eq) >= 2:
                result[eq[1]] = eq[0]
        return result

    def _equipment_locations(self):
        result = {}
        for commander in self.state.commanders:
            lord_id = commander.get("lord_id")
            position = commander.get("position")
            for eq in commander.get("equipment", []):
                if isinstance(eq, list) and len(eq) >= 2:
                    result[eq[0]] = (lord_id, position, eq[1])
        return result

    @staticmethod
    def _add_equipment_to_state(commander, equipment_id, slot):
        if commander is None:
            return
        current = []
        for eq in commander.get("equipment", []):
            if not isinstance(eq, list) or len(eq) < 2:
                continue
            if eq[1] == slot or eq[0] == equipment_id:
                continue
            current.append(eq)
        current.append([equipment_id, slot])
        commander["equipment"] = current
        commander["equipment_known"] = True

    @staticmethod
    def _remove_equipment_from_state(commander, equipment_id):
        if commander is None:
            return
        commander["equipment"] = [
            eq for eq in commander.get("equipment", [])
            if not (isinstance(eq, list) and eq and eq[0] == equipment_id)
        ]
        commander["equipment_known"] = True

    async def unequip(self):
        self._require_connected()
        async with self._action_lock:
            snapshot = self._read_snapshot()
            eligible_lids = self._eligible_lids_from_snapshot(snapshot)

            if not eligible_lids:
                self.log("The snapshot contains no commander with equipment eligible for removal.")
                self.progress("unequip", 0, 0, False, "No equipment to remove")
                return {"removed": 0, "failed": 0, "skipped": 0}

            # Refresh the inventory immediately before removing anything. This
            # makes the storage-space decision based on live server data.
            try:
                await self.refresh_inventory(silent=True)
            except Exception as exc:
                self.log(f"[WARNING] Live inventory refresh failed; using the latest known storage state: {exc}")

            storage = self.equipment_storage_status(snapshot)
            total_candidates = storage["required"]
            capacity = storage["capacity"]
            free = storage["free"]

            if capacity is None or free is None:
                self.log("[ERROR] Equipment storage capacity is unknown; unequip was blocked for safety.")
                self.progress("unequip", 0, total_candidates, False, "Storage data unavailable")
                return {
                    "removed": 0, "failed": 0, "skipped": 0, "blocked": True,
                    "reason": "storage_unknown", "required": total_candidates,
                    "free": free, "capacity": capacity,
                }

            if total_candidates > free:
                missing = total_candidates - free
                self.log(
                    f"[WARNING] Unequip blocked: {total_candidates} free slots are required, "
                    f"but only {free} are available ({missing} more needed)."
                )
                self.progress("unequip", 0, total_candidates, False, "Not enough storage space")
                return {
                    "removed": 0, "failed": 0, "skipped": 0, "blocked": True,
                    "reason": "not_enough_space", "required": total_candidates,
                    "free": free, "capacity": capacity, "missing": missing,
                }

            self.log(
                f"Storage preflight OK: {free} free slots, {total_candidates} required, "
                f"{free - total_candidates} will remain."
            )

            processed = 0
            removed = failed = skipped = 0
            self.log(f"Starting unequip: checking {len(eligible_lids)} saved commanders...")
            self.progress("unequip", 0, total_candidates, True, "Unequipping equipment")

            for lord_id in sorted(eligible_lids):
                commander = self.state.commander_by_id(lord_id)
                if commander is None:
                    skipped += 1
                    self.log(f"[WARNING] LID {lord_id}: currently unavailable, skipped.")
                    continue

                current_items = [
                    eq for eq in commander.get("equipment", [])
                    if isinstance(eq, list) and len(eq) >= 2
                ]

                if lord_id in self.state.moving_lord_ids or commander.get("busy"):
                    skipped += len(current_items) if current_items else 1
                    # Moving commanders were intentionally excluded from the
                    # storage preflight requirement, so they do not advance
                    # the actionable-item progress counter either.
                    self.log(f"[WARNING] LID {lord_id}: commander is moving, skipped.")
                    self.progress("unequip", processed, total_candidates, True, "Moving commander skipped")
                    continue

                if not commander.get("equipment_known", False):
                    skipped += 1
                    self.log(f"[WARNING] LID {lord_id}: current equipment data is unknown, skipped.")
                    continue

                if not current_items:
                    continue

                position = commander.get("position")
                pos_text = position + 1 if isinstance(position, int) else "?"
                self.log(f"Position {pos_text} / LID {lord_id}: removing {len(current_items)} items...")

                for eq in list(current_items):
                    equipment_id, slot = eq[0], eq[1]
                    live = self.state.commander_by_id(lord_id)
                    if live is None or lord_id in self.state.moving_lord_ids or live.get("busy"):
                        skipped += 1
                        processed += 1
                        self.log(f"[WARNING] Position {pos_text} / slot {slot}: commander started moving, skipped.")
                        self.progress("unequip", processed, total_candidates, True, "Skipped")
                        continue

                    try:
                        result, obj = await self._send_xt_and_wait(
                            "eeq", {"EID": equipment_id, "LID": lord_id, "E": 0}, timeout=15
                        )
                    except (TimeoutError, ConnectionError) as exc:
                        failed += 1
                        processed += 1
                        self.log(f"[ERROR] EID {equipment_id}: unequip failed: {exc}")
                        self.progress("unequip", processed, total_candidates, True, "Error")
                        await asyncio.sleep(EQUIP_DELAY_SECONDS)
                        continue

                    if result == 0:
                        removed += 1
                        self._remove_equipment_from_state(live, equipment_id)
                        self.state.adjust_inventory_used(+1)
                        self.log(
                            f"[SUCCESS] Position {pos_text} / slot {slot}: EID {equipment_id} removed "
                            f"({removed} total)."
                        )
                    else:
                        failed += 1
                        self.log(f"[ERROR] EID {equipment_id}: {ERRORS.get(result, result)} ({obj})")

                    processed += 1
                    self.progress("unequip", processed, total_candidates, True, f"{processed}/{total_candidates}")
                    await asyncio.sleep(EQUIP_DELAY_SECONDS)

            try:
                await self.refresh_inventory(silent=True)
            except Exception as exc:
                self.log(f"[WARNING] Final inventory verification failed: {exc}")

            storage_after = self.equipment_storage_status(snapshot)
            self.log(
                f"[SUCCESS] Unequip finished: {removed} removed, {skipped} skipped, {failed} failed. "
                f"Storage: {storage_after.get('used', '?')}/{storage_after.get('capacity', '?')}."
            )
            self.progress("unequip", total_candidates, total_candidates, False, "Unequip finished")
            return {
                "removed": removed, "failed": failed, "skipped": skipped, "blocked": False,
                "storage": storage_after,
            }

    async def restore(self):
        self._require_connected()
        async with self._action_lock:
            snapshot = self._read_snapshot()
            equipped_locations = self._equipment_locations()
            restored = already_ok = skipped = conflicts = failed = 0

            saved_commanders = sorted(
                snapshot.get("commanders", []),
                key=lambda c: (
                    c.get("position") is None,
                    c.get("position") if c.get("position") is not None else 10**9,
                ),
            )

            total_to_restore = sum(
                len(saved.get("equipment", []))
                for saved in saved_commanders
                if saved.get("equipment_known", False)
            )
            processed = 0

            self.log(
                f"Starting restore: {len(saved_commanders)} saved commanders, "
                f"checking {total_to_restore} saved items..."
            )
            self.progress("restore", 0, total_to_restore, True, "Restoring equipment")

            for saved in saved_commanders:
                position = saved.get("position")
                items = saved.get("equipment", [])
                if position is None or not saved.get("equipment_known", False):
                    skipped += len(items)
                    processed += len(items)
                    self.progress("restore", processed, total_to_restore, True, "Skipped")
                    continue

                target = self.state.commander_at_position(position)
                if target is None:
                    skipped += len(items)
                    processed += len(items)
                    self.log(f"[WARNING] Position {position + 1}: no available commander, skipped.")
                    self.progress("restore", processed, total_to_restore, True, "No commander")
                    continue

                target_lord_id = target.get("lord_id")
                if target_lord_id in self.state.moving_lord_ids or target.get("busy"):
                    skipped += len(items)
                    processed += len(items)
                    self.log(f"[WARNING] Position {position + 1}: commander is moving, skipped.")
                    self.progress("restore", processed, total_to_restore, True, "Moving")
                    continue

                saved_lid = saved.get("lord_id_at_save")
                self.log(
                    f"Position {position + 1} / LID {target_lord_id}: "
                    f"checking {len(items)} saved items..."
                )
                if saved_lid != target_lord_id:
                    self.log(
                        f"[WARNING] Position {position + 1}: LID {saved_lid} -> "
                        f"{target_lord_id}; restoring by saved VIS position."
                    )

                current_slots = self._slot_map(target)
                for item in sorted(items, key=lambda x: x.get("slot", 999)):
                    equipment_id = item.get("equipment_id")
                    slot = item.get("slot")
                    if equipment_id is None or slot is None:
                        processed += 1
                        self.progress("restore", processed, total_to_restore, True, "Invalid saved data")
                        continue

                    live_target = self.state.commander_at_position(position)
                    if live_target is None or live_target.get("lord_id") != target_lord_id:
                        skipped += 1
                        processed += 1
                        self.log(f"[WARNING] Position {position + 1} / slot {slot}: the position changed during restore.")
                        self.progress("restore", processed, total_to_restore, True, "Position changed")
                        continue
                    if target_lord_id in self.state.moving_lord_ids or live_target.get("busy"):
                        skipped += 1
                        processed += 1
                        self.log(f"[WARNING] Position {position + 1} / slot {slot}: commander is moving.")
                        self.progress("restore", processed, total_to_restore, True, "Moving")
                        continue

                    existing = current_slots.get(slot)
                    if existing == equipment_id:
                        already_ok += 1
                        processed += 1
                        self.log(
                            f"Position {position + 1} / slot {slot}: EID {equipment_id} is already correct."
                        )
                        self.progress("restore", processed, total_to_restore, True, f"{processed}/{total_to_restore}")
                        continue
                    if existing is not None:
                        conflicts += 1
                        processed += 1
                        self.log(
                            f"[WARNING] Position {position + 1} / slot {slot}: "
                            "another item is equipped; it will not be overwritten."
                        )
                        self.progress("restore", processed, total_to_restore, True, "Conflict")
                        continue
                    if equipment_id in equipped_locations:
                        conflicts += 1
                        processed += 1
                        self.log(
                            f"[WARNING] EID {equipment_id}: already equipped on another commander; "
                            "it will not be moved automatically."
                        )
                        self.progress("restore", processed, total_to_restore, True, "Conflict")
                        continue

                    try:
                        result, obj = await self._send_xt_and_wait(
                            "eeq", {"EID": equipment_id, "LID": target_lord_id, "E": 1}, timeout=15
                        )
                    except (TimeoutError, ConnectionError) as exc:
                        failed += 1
                        processed += 1
                        self.log(f"[ERROR] EID {equipment_id}: equip failed: {exc}")
                        self.progress("restore", processed, total_to_restore, True, "Error")
                        await asyncio.sleep(EQUIP_DELAY_SECONDS)
                        continue

                    if result == 0:
                        restored += 1
                        current_slots[slot] = equipment_id
                        equipped_locations[equipment_id] = (target_lord_id, position, slot)
                        self._add_equipment_to_state(live_target, equipment_id, slot)
                        self.state.adjust_inventory_used(-1)
                        self.log(
                            f"[SUCCESS] Position {position + 1} / slot {slot}: EID {equipment_id} restored "
                            f"({restored} restored)."
                        )
                    else:
                        failed += 1
                        self.log(f"[ERROR] EID {equipment_id}: {ERRORS.get(result, result)} ({obj})")

                    processed += 1
                    self.progress("restore", processed, total_to_restore, True, f"{processed}/{total_to_restore}")
                    await asyncio.sleep(EQUIP_DELAY_SECONDS)

            try:
                await self.refresh_inventory(silent=True)
            except Exception as exc:
                self.log(f"[WARNING] Final inventory verification failed: {exc}")

            storage_after = self.equipment_storage_status(snapshot)
            self.log(
                f"[SUCCESS] Restore finished: {restored} restored, {already_ok} already correct, "
                f"{skipped} skipped, {conflicts} conflicts, {failed} failed. "
                f"Storage: {storage_after.get('used', '?')}/{storage_after.get('capacity', '?')}."
            )
            self.progress("restore", total_to_restore, total_to_restore, False, "Restore finished")
            return {
                "restored": restored,
                "already_ok": already_ok,
                "skipped": skipped,
                "conflicts": conflicts,
                "failed": failed,
                "storage": storage_after,
            }
