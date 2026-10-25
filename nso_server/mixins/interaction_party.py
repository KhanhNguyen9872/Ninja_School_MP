from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from typing import Any

from ..config import MAX_NAME, MAX_PASSWORD, clean_text, password_digest
from ..models import Player, Room
from ..protocol import send_json

BOSS_ORDER_DENIALS = {1: "other_task", 2: "low_level", 3: "no_count"}
BOSS_ORDER_TIMEOUT = 5.0


class PartyInteractionMixin:
    async def start_boss_order(self, room: Room, player: Player) -> bool:
        """NSO_FINAL Char.orderTaskBoss: leader-only, all same-zone living members must qualify."""
        _, members = self.party_for(room, player.player_id)
        if members and members[0] != player.player_id:
            await send_json(player.writer, {"type": "task_denied", "code": "not_leader"})
            return False
        if any(probe.get("leader") == player.player_id for probe in room.task_probes.values()):
            await send_json(player.writer, {"type": "task_denied", "code": "busy"})
            return False
        expected = []
        for member_id in members:
            member = room.players.get(member_id)
            if member is None or member is player or member.hp <= 0                     or member.map_id != player.map_id or member.zone_id != player.zone_id:
                continue
            expected.append(member_id)
        probe_id = secrets.token_hex(6)
        room.task_probes[probe_id] = {"leader": player.player_id, "pending": set(expected),
                                      "members": list(expected)}
        if not expected:
            await self.finish_boss_order(room, probe_id)
            return True
        for member_id in expected:
            await send_json(room.players[member_id].writer,
                            {"type": "task_probe", "seq": room.sequence,
                             "data": probe_id, "player_id": player.player_id})
        self.spawn_task(self.expire_boss_order(room, probe_id))
        return True

    async def expire_boss_order(self, room: Room, probe_id: str) -> None:
        await asyncio.sleep(BOSS_ORDER_TIMEOUT)
        probe = room.task_probes.pop(probe_id, None)
        leader = room.players.get(probe["leader"]) if probe else None
        if leader is not None:
            await send_json(leader.writer, {"type": "task_denied", "code": "no_response"})

    async def record_boss_ack(self, room: Room, player: Player, data: str) -> bool:
        probe_id, _, status_text = data.partition(",")
        probe = room.task_probes.get(probe_id)
        if probe is None or player.player_id not in probe["pending"]:
            return False
        try:
            status = int(status_text)
        except ValueError:
            status = 1
        if status != 0:
            room.task_probes.pop(probe_id, None)
            leader = room.players.get(probe["leader"])
            if leader is not None:
                await send_json(leader.writer, {"type": "task_denied",
                                                "code": BOSS_ORDER_DENIALS.get(status, "other_task")})
            return False
        probe["pending"].discard(player.player_id)
        if not probe["pending"]:
            await self.finish_boss_order(room, probe_id)
        return True

    async def finish_boss_order(self, room: Room, probe_id: str) -> None:
        probe = room.task_probes.pop(probe_id, None)
        if probe is None:
            return
        for member_id in [probe["leader"]] + list(probe["members"]):
            member = room.players.get(member_id)
            if member is not None:
                await send_json(member.writer, {"type": "task_assign", "seq": room.sequence,
                                                "player_id": probe["leader"]})

    async def handle_party_interaction(
            self, room: Room, player: Player, target: Player | None,
            message: dict[str, Any], base: dict[str, Any], kind: str) -> bool:
        if kind == "party_invite":
            party_key, members = self.party_for(room, player.player_id)
            if members and members[0] != player.player_id:
                await send_json(player.writer, {"type": "error", "code": "party_leader_only"})
                return False
            _, target_party = self.party_for(room, target.player_id)
            if target_party:
                await send_json(player.writer, {"type": "error", "code": "party_member_busy"})
                return False
            if members and len(members) >= 6:
                await send_json(player.writer, {"type": "error", "code": "party_full"})
                return False
            room.party_invites[target.player_id] = player.player_id
            await send_json(target.writer, dict(base, type="party_invite"))
        elif kind == "party_accept":
            if room.party_invites.pop(player.player_id, "") != target.player_id:
                await send_json(player.writer, {"type": "error", "code": "party_invite_expired"})
                return False
            _, existing = self.party_for(room, player.player_id)
            if existing:
                await send_json(player.writer, {"type": "error", "code": "party_member_busy"})
                return False
            party_key, members = self.party_for(room, target.player_id)
            if not members:
                party_key = target.player_id
                members = room.parties.setdefault(party_key, [target.player_id])
            if members[0] != target.player_id:
                await send_json(player.writer, {"type": "error", "code": "party_leader_only"})
                return False
            if room.party_locked.get(party_key, False):
                await send_json(player.writer, {"type": "error", "code": "party_locked"})
                return False
            if len(members) >= 6:
                await send_json(player.writer, {"type": "error", "code": "party_full"})
                return False
            members.append(player.player_id)
            await self.send_party_roster(room, members)
        elif kind == "party_kick":
            party_key, members = self.party_for(room, player.player_id)
            if not members or members[0] != player.player_id:
                await send_json(player.writer, {"type": "error", "code": "party_leader_only"})
                return False
            if target.player_id not in members or target.player_id == player.player_id:
                await send_json(player.writer, {"type": "error", "code": "party_target_invalid"})
                return False
            members.remove(target.player_id)
            await send_json(target.writer, {"type": "party_closed", "seq": room.sequence})
            if len(members) < 2:
                await send_json(player.writer, {"type": "party_closed", "seq": room.sequence})
                room.parties.pop(party_key, None)
                room.party_locked.pop(party_key, None)
                room.party_dungeons.pop(party_key, None)
            else:
                await self.send_party_roster(room, members)
        elif kind == "party_leader":
            party_key, members = self.party_for(room, player.player_id)
            if not members or members[0] != player.player_id:
                await send_json(player.writer, {"type": "error", "code": "party_leader_only"})
                return False
            if target.player_id not in members or target.player_id == player.player_id:
                await send_json(player.writer, {"type": "error", "code": "party_target_invalid"})
                return False
            index = members.index(target.player_id)
            members[0], members[index] = members[index], members[0]
            room.parties.pop(party_key, None)
            locked = room.party_locked.pop(party_key, False)
            dungeon = room.party_dungeons.pop(party_key, 0)
            party_key = members[0]
            room.parties[party_key] = members
            if locked:
                room.party_locked[party_key] = True
            if dungeon:
                room.party_dungeons[party_key] = dungeon
            await self.send_party_roster(room, members)
        elif kind == "party_lock":
            party_key, members = self.party_for(room, player.player_id)
            if not members or members[0] != player.player_id:
                await send_json(player.writer, {"type": "error", "code": "party_leader_only"})
                return False
            room.party_locked[party_key] = str(message.get("data", "")).lower() == "true"
            await self.send_party_roster(room, members)
        elif kind == "dungeon_open":
            try:
                option = int(str(message.get("data", "0")))
            except ValueError:
                option = 0
            party_key, members = self.party_for(room, player.player_id)
            if not members:
                party_key, members = player.player_id, [player.player_id]
            if members[0] != player.player_id:
                await send_json(player.writer, {"type": "error", "code": "party_leader_only"})
                return False
            if player.player_id != room.owner_id:
                await send_json(player.writer, {"type": "error", "code": "owner_world_only"})
                return False
            if option == 4 and len(members) > 1:
                await send_json(player.writer, {"type": "error", "code": "dungeon_solo_only"})
                return False
            for member_id in members:
                member = room.players.get(member_id)
                if member is None or not self.dungeon_level_eligible(
                        int(member.appearance.get("level", 1)), option):
                    await send_json(player.writer,
                                    {"type": "error", "code": "dungeon_level_mismatch"})
                    return False
            now = int(time.time() * 1000)
            run = next((value for value in room.dungeon_runs.values()
                        if value.get("owner") == player.player_id and not value.get("finished")
                        and int(value.get("deadline", 0)) > now), None)
            if run is None:
                run = {"id": secrets.token_hex(12), "owner": player.player_id,
                       "members": list(members), "level": option - 1,
                       "deadline": now + 3600000, "finished": False}
                room.dungeon_runs[run["id"]] = run
            room.party_dungeons[party_key] = int(run["level"]) + 1
            for member_id in run["members"]:
                member = room.players.get(member_id)
                if member is not None:
                    await send_json(member.writer, {"type": "dungeon_admit",
                                                    "seq": room.sequence,
                                                    "category": int(run["level"]) + 1,
                                                    "data": run["id"],
                                                    "leader_id": player.player_id})
        elif kind == "party_leave":
            for key, members in list(room.parties.items()):
                if player.player_id not in members:
                    continue
                members.remove(player.player_id)
                await send_json(player.writer, {"type": "party_closed", "seq": room.sequence})
                if len(members) < 2:
                    for member in members:
                        if member in room.players:
                            await send_json(room.players[member].writer,
                                            {"type": "party_closed", "seq": room.sequence})
                    room.parties.pop(key, None)
                    room.party_locked.pop(key, None)
                    room.party_dungeons.pop(key, None)
                else:
                    if key == player.player_id:
                        room.parties.pop(key, None)
                        locked = room.party_locked.pop(key, False)
                        dungeon = room.party_dungeons.pop(key, 0)
                        key = members[0]
                        room.parties[key] = members
                        if locked:
                            room.party_locked[key] = True
                        if dungeon:
                            room.party_dungeons[key] = dungeon
                    await self.send_party_roster(room, members)
                break
        elif kind == "task_boss_order":
            return await self.start_boss_order(room, player)
        elif kind == "task_boss_ack":
            return await self.record_boss_ack(room, player, str(message.get("data", "")))
        elif kind == "party_chat":
            text = clean_text(message.get("data"), 300)
            members = next((members for members in room.parties.values()
                            if player.player_id in members), [])
            for member in members:
                if member in room.players:
                    await send_json(room.players[member].writer,
                                    dict(base, type="party_chat", text=text))
        return True
