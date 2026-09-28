"""Party and clan state helpers."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from ..config import MAX_NAME, clean_text
from ..models import Player, Room
from ..protocol import encode_frame

class SocialMixin:
    def new_room_id(self) -> str:
        while True:
            room_id = secrets.token_urlsafe(5).replace("-", "").replace("_", "")[:8].upper()
            if room_id and room_id not in self.rooms:
                return room_id

    @staticmethod
    def unique_room_name(room: Room, player: Player, requested: str) -> str:
        base = clean_text(requested, MAX_NAME) or "player"
        used = {name.casefold() for name in room.reserved_names if name}
        used.update(candidate.name.casefold() for candidate in room.players.values()
                    if candidate is not player and candidate.name)
        if base.casefold() not in used:
            return base
        suffix = 1
        while True:
            tail = "_" + str(suffix)
            candidate = base[:max(1, MAX_NAME - len(tail))] + tail
            if candidate.casefold() not in used:
                return candidate
            suffix += 1

    @staticmethod
    def unique_clan_name(room: Room, player: Player, requested: str) -> str:
        """Allocate a room-visible clan identity without merging same names.

        A player's existing clan keeps its assigned alias across repeated
        appearance packets. A distinct locally-owned clan with the same name
        receives `_1`, `_2`, ... just like duplicate character names.
        """
        existing = SocialMixin.clan_for_player(room, player.player_id)
        if existing is not None:
            return str(existing.get("name", requested))
        base = clean_text(requested, MAX_NAME) or "Offline"
        used = {str(clan.get("name", "")).casefold()
                for clan in room.clans.values() if clan.get("name")}
        if base.casefold() not in used:
            return base
        suffix = 1
        while True:
            tail = "_" + str(suffix)
            candidate = base[:max(1, MAX_NAME - len(tail))] + tail
            if candidate.casefold() not in used:
                return candidate
            suffix += 1

    @staticmethod
    def party_for(room: Room, player_id: str) -> tuple[str, list[str]]:
        for key, members in room.parties.items():
            if player_id in members:
                return key, members
        return "", []

    @staticmethod
    def dungeon_level_eligible(level: int, option: int) -> bool:
        required = (35, 45, 55, 65, 75, 95)
        if option < 1 or option > len(required):
            return False
        return level // 10 == required[option - 1] // 10 \
            or option == 5 and 70 <= level <= 89 \
            or option == 6 and level >= 90

    async def send_party_roster(self, room: Room, members: list[str]) -> None:
        if not members:
            return
        leader = members[0]
        roster = [self.player_snapshot(room.players[member]) for member in members
                  if member in room.players]
        payload = {"type": "party_roster", "seq": room.sequence,
                   "leader_id": leader, "locked": bool(room.party_locked.get(leader, False)),
                   "members": roster}
        # Publish the same immutable roster to every existing member before
        # waiting on any one slow J2ME socket. Sequential send_json/drain made
        # a stalled leader prevent older members from seeing the new joiner.
        frame = encode_frame(payload)
        recipients: list[Any] = []
        for member in members:
            participant = room.players.get(member)
            if participant is not None:
                try:
                    participant.writer.write(frame)
                    recipients.append(participant.writer)
                except (ConnectionError, OSError):
                    pass
        if recipients:
            await asyncio.gather(*(writer.drain() for writer in recipients),
                                 return_exceptions=True)

    @staticmethod
    def clan_exp_next(level: int) -> int:
        level = max(1, level)
        result = 2000
        for current in range(1, level):
            if current == 1:
                result = 3720
            elif current < 10:
                result = ((result // current) + 310) * (current + 1)
            elif current < 20:
                result = ((result // current) + 620) * (current + 1)
            else:
                result = ((result // current) + 930) * (current + 1)
        return result

    @staticmethod
    def clan_coin_up(level: int) -> int:
        return ((max(1, level) - 1) // 10 + 1) * 100000 + 500000

    def ensure_clan(self, room: Room, player: Player, requested: str) -> dict[str, Any]:
        name = self.unique_clan_name(room, player, requested)
        key = name.casefold()
        clan = room.clans.get(key)
        if clan is None:
            clan = {"name": name, "leader_id": player.player_id, "level": 1,
                    "exp": 0, "coin": 0, "item_level": 0, "alert": "",
                    "members": {}, "pets": [0, 0, 0], "items": {},
                    "territory": {"active": False, "deadline": 0, "points": 0,
                                  "entries": 1, "use_card": 1},
                    "war": {"active": False, "opponent": "", "own_points": 0,
                            "enemy_points": 0, "deadline": 0}}
            room.clans[key] = clan
        members = clan.setdefault("members", {})
        clan.setdefault("items", {})
        member = members.setdefault(player.player_id, {})
        member.update({"name": player.name,
                       "class_id": int(player.appearance.get("class_id", 0)),
                       "level": int(player.appearance.get("level", 1)),
                       "role": 4 if player.player_id == clan.get("leader_id")
                       else int(member.get("role", 0)),
                       "points": int(member.get("points", 0))})
        player.clan_name = str(clan.get("name", name))
        return clan

    @staticmethod
    def clan_for_player(room: Room, player_id: str) -> dict[str, Any] | None:
        return next((clan for clan in room.clans.values()
                     if player_id in clan.get("members", {})), None)

    def clan_payload(self, room: Room, clan: dict[str, Any]) -> dict[str, Any]:
        rows: list[str] = []
        members = clan.get("members", {})
        for player_id, member in members.items():
            safe = clean_text(member.get("name"), MAX_NAME).replace(":", " ").replace(";", " ")
            rows.append(f'{player_id}:{safe}:{int(member.get("class_id", 0))}:'
                        f'{int(member.get("level", 1))}:{int(member.get("role", 0))}:'
                        f'{int(member.get("points", 0))}:'
                        f'{1 if player_id in room.players else 0}')
        leader_id = str(clan.get("leader_id", ""))
        leader = room.players.get(leader_id)
        deputy = next((str(member.get("name", "")) for member in members.values()
                       if int(member.get("role", 0)) == 3), "")
        inventory = ";".join(
            f'{int(row.get("item", key))},{int(row.get("quantity", 0))},'
            f'{1 if row.get("locked", False) else 0},{int(row.get("upgrade", 0))},'
            f'{int(row.get("sys", 0))},{int(row.get("expire", -1))},'
            f'{clean_text(row.get("options", ""), 512)}'
            for key, row in clan.get("items", {}).items()
            if int(row.get("quantity", 0)) > 0)
        return {"type": "clan_state", "seq": room.sequence,
                "clan_name": str(clan.get("name", "")), "owner_id": leader_id,
                "name": leader.name if leader is not None else "", "peer_name": deputy,
                "level": int(clan.get("level", 1)), "quantity": int(clan.get("exp", 0)),
                "amount": int(clan.get("coin", 0)),
                "category": int(clan.get("item_level", 0)),
                "text": clean_text(clan.get("alert"), 300),
                "data": ";".join(rows),
                "inventory": inventory,
                "payload": {"pets": list(clan.get("pets", [0, 0, 0])),
                            "territory": dict(clan.get("territory", {})),
                            "war": dict(clan.get("war", {}))}}

    async def broadcast_clan(self, room: Room, clan: dict[str, Any]) -> None:
        payload = self.clan_payload(room, clan)
        frame = encode_frame(payload); pending = []
        for player_id in list(clan.get("members", {})):
            member = room.players.get(player_id)
            if member is not None:
                member.writer.write(frame); pending.append(member.writer.drain())
        if pending: await asyncio.gather(*pending, return_exceptions=True)

