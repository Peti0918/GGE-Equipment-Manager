"""Tracks when our own commanders actually get home, using the "gam" pushes
the server sends as armies change state - instead of the rough "2x outbound
travel time" guess. Reference: protocols.js's Movement class + newMovement().

An outbound attack (M.OID == us, M.TID == the barron) doesn't tell us when
the commander is free - only once the return leg shows up (M.TID == us, the
commander now heading home) do we know the real, server-confirmed travel
time.  Falls back to the original 2x-estimate if that "returning" push never
arrives within FALLBACK_FREE_SECONDS, so a field-mapping mistake here can't
strand a commander as "busy" forever.
"""

import json
import time

FALLBACK_FREE_SECONDS = 20 * 60


def _log(*parts):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}]", *parts)


class MovementTracker:
    def __init__(self, player_id):
        self.player_id = player_id
        self._free_at = {}   # lord_id -> monotonic timestamp
        self._outbound = {}  # lord_id -> monotonic timestamp of send, for the fallback
        self._dumped_raw = False

    def free_at(self, lord_id):
        return self._free_at.get(lord_id, 0)

    def mark_sent(self, lord_id, travel_seconds):
        now = time.monotonic()
        self._outbound[lord_id] = now
        # rough estimate, overwritten below as soon as we see the real return-leg push
        self._free_at[lord_id] = now + max(1, 2 * travel_seconds)

    def mark_externally_busy(self, lord_id):
        """LORD_IS_USED told us this commander is out on a mission we never
        sent (from before this run started, or sent by something else on the
        account) - we don't know its real travel time, so watch for its
        eventual "gam" return push like any other, and fall back to
        FALLBACK_FREE_SECONDS if that never comes."""
        now = time.monotonic()
        self._outbound.setdefault(lord_id, now)
        self._free_at[lord_id] = now + FALLBACK_FREE_SECONDS

    def handle_gam(self, obj):
        if not self._dumped_raw and obj.get("M"):
            self._dumped_raw = True
            _log(f"RAW 'gam' first movement entry: {json.dumps(obj['M'][0])[:1500]}")

        for entry in obj.get("M", []):
            self._handle_movement(entry)

    def _handle_movement(self, entry):
        m = entry.get("M", {})
        lord_id = entry.get("UM", {}).get("L", {}).get("ID")
        if lord_id is None or lord_id not in self._outbound:
            return

        # the return leg: we own the destination now, not the barron
        if m.get("TID") != self.player_id:
            return

        travel_seconds = max(1, m.get("TT", 0) - m.get("PT", 0))
        self._free_at[lord_id] = time.monotonic() + travel_seconds
        del self._outbound[lord_id]

    def sweep_fallback(self):
        """Drop tracking for anything that never got a "returning" push, so
        it doesn't sit in _outbound forever - the original estimate already
        set a free_at for it, this just stops us re-checking it forever."""
        now = time.monotonic()
        stale = [lord_id for lord_id, sent_at in self._outbound.items()
                 if now - sent_at > FALLBACK_FREE_SECONDS]
        for lord_id in stale:
            del self._outbound[lord_id]
