from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from ..config import MAX_NAME, MAX_PASSWORD, clean_text, password_digest
from ..models import Player, Room
from ..protocol import send_json

class PartyInteractionMixin:
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
            room.party_dungeons[party_key] = option
            for member_id in members:
                member = room.players.get(member_id)
                if member is not None:
                    await send_json(member.writer, {"type": "dungeon_admit",
                                                    "seq": room.sequence,
                                                    "category": option,
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
        elif kind == "party_chat":
            text = clean_text(message.get("data"), 300)
            members = next((members for members in room.parties.values()
                            if player.player_id in members), [])
            for member in members:
                if member in room.players:
                    await send_json(room.players[member].writer,
                                    dict(base, type="party_chat", text=text))
        return True
