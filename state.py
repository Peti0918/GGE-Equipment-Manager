"""Commander/equipment state for the Goodgame Empire equipment helper.

This GUI-focused version intentionally contains no robber-baron or troop logic.
It merges commander information from the normal lord list (gli) and active
movements (gam), so commanders that are currently away can still be tracked.

Equipment storage is tracked from two protocol sources:
- gbd -> esl: E = free equipment slots, TE = total equipment slots
- gei -> I:   current equipment inventory entries (len(I) = used slots)
"""


class GameState:
    def __init__(self):
        self.player_id = None
        self.commanders = []
        self._commanders_by_id = {}
        self.moving_lord_ids = set()
        self._pending_gam = None

        # Equipment storage.
        self.inventory_items = []
        self.inventory_used = None
        self.inventory_capacity = None
        self.inventory_free = None

        # Kept for diagnostics / future gem-storage support.
        self.gem_capacity = None
        self.gem_free = None

    @staticmethod
    def _safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def handle(self, cmd, result, obj):
        if result != 0 or not isinstance(obj, dict):
            return

        if cmd == "gbd":
            # gbd is a bundle of normal protocol payloads. Feed each child back
            # through the same handler so esl/gli/gpi/etc. are all captured.
            for key, value in obj.items():
                self.handle(key, 0, value)
            return

        if cmd == "gpi":
            self.player_id = self._safe_int(obj.get("PID"))
            if self._pending_gam is not None:
                pending = self._pending_gam
                self._pending_gam = None
                self._handle_gam(pending)
        elif cmd == "gli":
            self._handle_gli(obj)
        elif cmd == "gam":
            self._handle_gam(obj)
        elif cmd == "esl":
            self._handle_esl(obj)
        elif cmd == "gei":
            self._handle_gei(obj)

    def _handle_esl(self, obj):
        """Storage limits sent in the login state bundle.

        Observed structure:
            {"E": 92, "G": 326, "TE": 402, "TG": 482}
        where E/TE are free/total equipment slots.
        """
        total_equipment = self._safe_int(obj.get("TE"))
        free_equipment = self._safe_int(obj.get("E"))
        total_gems = self._safe_int(obj.get("TG"))
        free_gems = self._safe_int(obj.get("G"))

        if total_equipment is not None:
            self.inventory_capacity = total_equipment
        if free_equipment is not None:
            self.inventory_free = free_equipment
        if total_gems is not None:
            self.gem_capacity = total_gems
        if free_gems is not None:
            self.gem_free = free_gems

        # esl alone is enough to infer current usage. gei will later verify it.
        if self.inventory_capacity is not None and self.inventory_free is not None:
            self.inventory_used = max(0, self.inventory_capacity - self.inventory_free)

    def _handle_gei(self, obj):
        items = obj.get("I")
        if not isinstance(items, list):
            return

        self.inventory_items = items
        self.inventory_used = len(items)
        if self.inventory_capacity is not None:
            self.inventory_free = max(0, self.inventory_capacity - self.inventory_used)

    def adjust_inventory_used(self, delta):
        """Apply a confirmed equip/unequip operation to the local storage count.

        delta > 0: an item was unequipped and entered storage.
        delta < 0: an item was equipped and left storage.
        """
        delta = self._safe_int(delta)
        if delta is None:
            return

        if self.inventory_used is not None:
            self.inventory_used = max(0, self.inventory_used + delta)
            if self.inventory_capacity is not None:
                self.inventory_used = min(self.inventory_capacity, self.inventory_used)

        if self.inventory_capacity is not None and self.inventory_used is not None:
            self.inventory_free = max(0, self.inventory_capacity - self.inventory_used)
        elif self.inventory_free is not None:
            self.inventory_free = max(0, self.inventory_free - delta)

    def _merge_lord(self, lord_obj, source):
        if not isinstance(lord_obj, dict):
            return

        lord_id = self._safe_int(lord_obj.get("ID"))
        if lord_id is None:
            return

        commander = self._commanders_by_id.setdefault(
            lord_id,
            {
                "lord_id": lord_id,
                "position": None,
                "name": "",
                "equipment": [],
                "equipment_known": False,
                "sources": set(),
                "busy": False,
            },
        )

        if "VIS" in lord_obj:
            commander["position"] = self._safe_int(lord_obj.get("VIS"))
        if "N" in lord_obj and lord_obj.get("N") is not None:
            commander["name"] = str(lord_obj.get("N") or "")
        if "EQ" in lord_obj:
            commander["equipment"] = lord_obj.get("EQ") or []
            commander["equipment_known"] = True

        commander["sources"].add(source)
        commander["busy"] = lord_id in self.moving_lord_ids
        self._refresh_commanders()

    def _refresh_commanders(self):
        for lord_id, commander in self._commanders_by_id.items():
            commander["busy"] = lord_id in self.moving_lord_ids

        def sort_key(commander):
            pos = commander.get("position")
            if isinstance(pos, int) and pos >= 0:
                return (0, pos, commander["lord_id"])
            return (1, 10**9, commander["lord_id"])

        self.commanders = sorted(self._commanders_by_id.values(), key=sort_key)

    def _handle_gli(self, obj):
        for entry in obj.get("C", []):
            self._merge_lord(entry, "gli")

    def _handle_gam(self, obj):
        if self.player_id is None:
            self._pending_gam = obj
            return

        moving_now = set()
        own_lords = []

        for entry in obj.get("M", []):
            if not isinstance(entry, dict):
                continue
            movement = entry.get("M", {})
            if self._safe_int(movement.get("OID")) != self.player_id:
                continue

            lord_obj = entry.get("UM", {}).get("L", {})
            lord_id = self._safe_int(lord_obj.get("ID"))
            if lord_id is None:
                continue

            moving_now.add(lord_id)
            own_lords.append(lord_obj)

        self.moving_lord_ids = moving_now
        for lord_obj in own_lords:
            self._merge_lord(lord_obj, "gam")
        self._refresh_commanders()

    def commander_by_id(self, lord_id):
        lord_id = self._safe_int(lord_id)
        if lord_id is None:
            return None
        return self._commanders_by_id.get(lord_id)

    def commander_at_position(self, position):
        position = self._safe_int(position)
        if position is None:
            return None
        for commander in self.commanders:
            if commander.get("position") == position:
                return commander
        return None
