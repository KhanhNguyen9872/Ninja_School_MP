"""Debounced persistence and state serialization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from typing import Any

from ..config import MAX_NAME, clean_text
from ..models import Player, Room

class PersistenceMixin:
    def load_state(self) -> None:
        if not self.state_file:
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as stream:
                data = json.load(stream)
            saved = data.get("rooms", {}) if isinstance(data, dict) else {}
            worlds = data.get("worlds", {}) if isinstance(data, dict) else {}
            # A room is a live-owner lease, not a durable server object.  A
            # process restart has no connected owners, therefore every prior
            # room is stale and its code must become reusable immediately.
            stale = (len(saved) if isinstance(saved, dict) else 0) + (
                len(worlds) if isinstance(worlds, dict) else 0)
            self.saved_rooms = {}
            self.saved_worlds = {}
            self._saved_row_index = {}
            if stale:
                self.log("stale_rooms_discarded", entries=stale)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            print(f"warning: ignoring invalid state file {self.state_file}", flush=True)

    def _snapshot_state(self) -> dict[str, Any]:
        # Persist only rooms which currently have a live owner.  Disconnected
        # guest snapshots may remain resumable while that owner is connected,
        # but a closed room never survives into the next snapshot.
        rooms: dict[str, list[dict[str, Any]]] = {}
        worlds: dict[str, dict[str, Any]] = {}
        for room_id, room in self.rooms.items():
            if room.owner_id not in room.players:
                continue
            room.ensure_world()
            self.prune_expired_drops(room)
            saved = {str(row.get("player_id", "")): row
                     for row in self.saved_rooms.get(room_id, [])}
            for player in room.players.values():
                saved[player.player_id] = self.persistent_player_snapshot(player)
            rooms[room_id] = list(saved.values())
            worlds[room_id] = {"sequence": room.sequence,
                               "password_hash": room.password_hash,
                               "owner_id": room.owner_id,
                               "cheat_enabled": room.cheat_enabled,
                               "mobs": list(room.mobs.values()),
                               "drops": list(room.drops.values()),
                               "shinwa": list(room.shinwa.values()),
                               "next_shinwa_id": room.next_shinwa_id,
                               "parties": room.parties,
                               "party_locked": room.party_locked,
                               "party_dungeons": room.party_dungeons,
                               "dungeon_runs": room.dungeon_runs,
                               "trades": room.trades,
                               "friends": room.friends,
                               "friend_requests": room.friend_requests,
                               "clan_invites": room.clan_invites,
                               "cuu_sat": room.cuu_sat,
                               "reserved_names": room.reserved_names,
                               "lucky_bets": room.lucky_bets,
                               "chan_le_bets": room.chan_le_bets,
                               "rankings": room.rankings,
                               "clans": room.clans,
                               "pending_deliveries": room.pending_deliveries,
                               "clan_contributions": room.clan_contributions}
        return {"version": 4, "saved_at": int(time.time()), "rooms": rooms,
                "worlds": worlds}

    def _write_state(self, data: dict[str, Any]) -> None:
        if not self.state_file:
            return
        directory = os.path.dirname(os.path.abspath(self.state_file))
        os.makedirs(directory, exist_ok=True)
        temporary = self.state_file + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, self.state_file)

    async def _save_after_debounce(self) -> None:
        try:
            await asyncio.sleep(0.25)
            data = self._snapshot_state()
            await asyncio.get_running_loop().run_in_executor(
                self._save_executor, self._write_state, data)
        finally:
            self._save_task = None

    def save_state(self) -> None:
        """Coalesce hot movement writes and execute disk I/O off the event loop."""
        if not self.state_file:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_state(self._snapshot_state())
            return
        if self._save_task is None:
            self._save_task = loop.create_task(self._save_after_debounce())

    @staticmethod
    def prune_expired_drops(room: Room) -> int:
        now = int(time.time() * 1000)
        expired = [drop_id for drop_id, row in room.drops.items()
                   if int(row.get("expires_at", 0)) > 0 and now >= int(row["expires_at"])]
        for drop_id in expired:
            room.drops.pop(drop_id, None)
        return len(expired)

    async def flush_state(self) -> None:
        """Deterministic test/shutdown boundary for the coalesced I/O worker."""
        pending = self._save_task
        if pending is not None:
            await pending
        elif self.state_file:
            data = self._snapshot_state()
            await asyncio.get_running_loop().run_in_executor(
                self._save_executor, self._write_state, data)

    @staticmethod
    def projected_appearance(appearance: dict[str, Any]) -> dict[str, Any]:
        result = dict(appearance)
        source = appearance.get("combat", {})
        if isinstance(source, dict) and source.get("v") == 1:
            combat = dict(source)
            now = int(time.monotonic() * 1000)
            elapsed = max(0, now - int(source.get("_at", now)))
            for key in ("locked_ms", "miss_ms", "burn_ms", "stealth_ms", "invisible_ms", "reduction_ms"):
                combat[key] = max(0, int(source.get(key, 0)) - elapsed)
            combat["_at"] = now
            result["combat"] = combat
        return result

    @staticmethod
    def player_snapshot(player: Player) -> dict[str, Any]:
        return {"player_id": player.player_id, "name": player.name, "map": player.map_id,
                "zone": player.zone_id,
                "x": player.x, "y": player.y, "hp": player.hp,
                "actor_id": getattr(player, "actor_id",
                                    PersistenceMixin.actor_id_for(player.player_id)),
                "appearance": PersistenceMixin.projected_appearance(getattr(player, "appearance", {})),
                "clone": dict(getattr(player, "clone", {})),
                "cheat_enabled": bool(getattr(player, "cheat_enabled", False))}

    @staticmethod
    def persistent_player_snapshot(player: Player) -> dict[str, Any]:
        snapshot = PersistenceMixin.player_snapshot(player)
        snapshot["resume_token_hash"] = clean_text(
            getattr(player, "resume_token_hash", ""), 128)
        return snapshot

    @staticmethod
    def actor_id_for(player_id: str) -> int:
        try:
            seed = int(player_id[:8], 16) if "#" not in player_id else int(
                hashlib.sha256(player_id.encode("utf-8")).hexdigest()[:8], 16)
        except ValueError:
            seed = sum(ord(char) for char in player_id)
        return 1000000000 + seed % 800000000

    def room_actor_id(self, room: Room, identity: str, preferred: int = 0) -> int:
        """Allocate an unambiguous wire ID within the live owner's room lease.

        Hashes are compatibility preferences, not unique identities. Reserve
        primary and clone IDs together on join; keep them when guests leave so
        reconnects and pending events never bind an old ID to another player.
        Rooms intentionally disappear when their owner leaves or on restart.
        """
        existing = room.actor_ids.get(identity)
        if existing is not None:
            return existing
        used = set(room.actor_ids.values())
        used.update(p.actor_id for p in room.players.values()
                    if p.player_id != identity and p.actor_id)
        candidate = preferred if 0 < preferred < 2147483647 else self.actor_id_for(identity)
        while candidate in used:
            candidate = 1000000000 + (candidate - 1000000000 + 1) % 800000000
        room.actor_ids[identity] = candidate
        return candidate

    def saved_player_row(self, room_id: str, player_id: str) -> dict[str, Any] | None:
        rows = self.saved_rooms.get(room_id, [])
        key = (room_id, player_id)
        index = self._saved_row_index.get(key)
        if index is not None and index < len(rows) \
                and rows[index].get("player_id") == player_id:
            return rows[index]
        for index, row in enumerate(rows):
            if row.get("player_id") == player_id:
                self._saved_row_index[key] = index
                return row
        return None

    def forget_saved_room(self, room_id: str) -> None:
        self.saved_rooms.pop(room_id, None)
        self.saved_worlds.pop(room_id, None)
        stale = [key for key in self._saved_row_index if key[0] == room_id]
        for key in stale:
            self._saved_row_index.pop(key, None)

    def remember_player(self, player: Player) -> None:
        if not player.room_id:
            return
        rows = self.saved_rooms.setdefault(player.room_id, [])
        key = (player.room_id, player.player_id)
        index = self._saved_row_index.get(key)
        if index is not None and index < len(rows) \
                and rows[index].get("player_id") == player.player_id:
            rows[index] = self.persistent_player_snapshot(player)
            return
        rows.append(self.persistent_player_snapshot(player))
        self._saved_row_index[key] = len(rows) - 1

    @staticmethod
    def clean_int_list(value: Any, count: int, minimum: int, maximum: int) -> list[int]:
        source = value if isinstance(value, list) else []
        result: list[int] = []
        for index in range(count):
            try:
                item = int(source[index]) if index < len(source) else -1
            except (TypeError, ValueError):
                item = -1
            result.append(max(minimum, min(item, maximum)))
        return result

    def clean_appearance(self, message: dict[str, Any]) -> dict[str, Any]:
        def number(key: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(message.get(key, default))
            except (TypeError, ValueError):
                value = default
            return max(minimum, min(value, maximum))
        source = message.get("combat")
        combat: dict[str, Any] = {}
        if isinstance(source, dict) and source.get("v") == 1:
            def combat_number(key: str, default: int = 0, maximum: int = 2000000000,
                              minimum: int = 0) -> int:
                try:
                    value = int(source.get(key, default))
                except (TypeError, ValueError):
                    value = default
                return max(minimum, min(value, maximum))
            combat = {key: combat_number(key) for key in (
                "damage2", "fatal_damage", "fatal_percent", "support_fatal", "stealth_fatal",
                "atk_fire", "atk_ice", "atk_wind", "atk_pct54", "atk_pct55", "atk_pct56",
                "res_fire_pct", "res_ice_pct", "res_wind_pct", "res_fire_flat",
                "res_ice_flat", "res_wind_flat", "flat113", "flat103", "pvp_reduce",
                "fatal_reduce", "fatal_resist", "reduction_percent", "pet_dame_down")}
            combat.update({key: combat_number(key, maximum=600000) for key in (
                "locked_ms", "miss_ms", "burn_ms", "stealth_ms", "invisible_ms", "reduction_ms")})
            combat.update(v=1, skill=combat_number("skill", -1, 127, -1),
                          attack_type=combat_number("attack_type", maximum=4),
                          dx=combat_number("dx", maximum=2000), dy=combat_number("dy", maximum=2000),
                          mana=combat_number("mana", maximum=65535),
                          cooldown=combat_number("cooldown", maximum=3600000),
                          bind_ms=combat_number("bind_ms", maximum=600000),
                          free_mana=combat_number("free_mana", maximum=1), _at=int(time.monotonic() * 1000))
        return {"name": clean_text(message.get("name"), MAX_NAME),
                "class_id": number("class_id", 0, 0, 6),
                "gender": number("gender", 0, 0, 1),
                "head": number("head", 0, -1, 32767),
                "weapon": number("weapon", -1, -1, 32767),
                "body": number("body", 0, -1, 32767),
                "leg": number("leg", 0, -1, 32767),
                "level": number("level", 1, 1, 200),
                "combat": combat,
                "max_hp": number("max_hp", 1, 1, 2000000000),
                "fashion": self.clean_int_list(message.get("fashion"), 10, -1, 32767),
                "mount_ids": self.clean_int_list(message.get("mount_ids"), 5, -1, 32767),
                "mount_upgrades": self.clean_int_list(message.get("mount_upgrades"), 5, -1, 127),
                "mount_systems": self.clean_int_list(message.get("mount_systems"), 5, -1, 127),
                "pet_template": number("pet_template", 0, 0, 32767),
                "pet_boss": bool(message.get("pet_boss", False)),
                "bijuu_template": number("bijuu_template", 0, 0, 32767),
                "bijuu_boss": bool(message.get("bijuu_boss", False)),
                "mp": number("mp", 0, 0, 2000000000),
                "max_mp": number("max_mp", 1, 1, 2000000000),
                "speed": number("speed", 6, 0, 127),
                "res_fire": number("res_fire", 0, 0, 32767),
                "res_ice": number("res_ice", 0, 0, 32767),
                "res_wind": number("res_wind", 0, 0, 32767),
                "damage": number("damage", 1, 0, 2000000000),
                "damage_down": number("damage_down", 0, 0, 2000000000),
                "exactly": number("exactly", 0, 0, 32767),
                "miss": number("miss", 0, 0, 32767),
                "fatal": number("fatal", 0, 0, 32767),
                "react_damage": number("react_damage", 0, 0, 32767),
                "sys_up": number("sys_up", 0, 0, 32767),
                "sys_down": number("sys_down", 0, 0, 32767),
                "equipment_ids": self.clean_int_list(message.get("equipment_ids"), 16, -1, 32767),
                "equipment_upgrades": self.clean_int_list(message.get("equipment_upgrades"), 16, -1, 127),
                "equipment_systems": self.clean_int_list(message.get("equipment_systems"), 16, -1, 127),
                "fashion_ids": self.clean_int_list(message.get("fashion_ids"), 16, -1, 32767),
                "fashion_upgrades": self.clean_int_list(message.get("fashion_upgrades"), 16, -1, 127),
                "fashion_systems": self.clean_int_list(message.get("fashion_systems"), 16, -1, 127),
                "clan_name": clean_text(message.get("clan_name"), MAX_NAME)}

    def clean_clone(self, message: dict[str, Any], player: Player) -> dict[str, Any]:
        active = bool(message.get("active", False))
        appearance = message.get("appearance", {})
        cleaned = self.clean_appearance(appearance if isinstance(appearance, dict) else {})
        return {"active": active,
                "map": int(message.get("map", player.map_id)),
                "zone": max(0, int(message.get("zone", player.zone_id))),
                "x": int(message.get("x", player.x)),
                "y": int(message.get("y", player.y)),
                "hp": max(0, int(message.get("hp", 0))),
                "appearance": cleaned}

