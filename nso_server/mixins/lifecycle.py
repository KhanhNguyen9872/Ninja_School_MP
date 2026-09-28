"""Room membership, reconnect and disconnect lifecycle."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from ..config import MAX_NAME, MAX_ROOM_PLAYERS, clean_text, password_digest
from ..models import Player
from ..protocol import send_json

class LifecycleMixin:
    async def disconnect(self, player: Player | None) -> None:
        if player is None:
            return
        self.players.pop(player.player_id, None)
        room = self.rooms.get(player.room_id or "")
        if room is not None:
            if room.owner_id == player.player_id:
                guests = [guest for guest in room.players.values()
                          if guest.player_id != player.player_id]
                for guest in guests:
                    try:
                        await send_json(guest.writer, {"type": "error",
                                                       "code": "room_owner_left"})
                    except (ConnectionError, OSError):
                        pass
                    self.players.pop(guest.player_id, None)
                    guest.room_id = None
                    try:
                        guest.writer.close()
                        await guest.writer.wait_closed()
                    except (ConnectionError, OSError):
                        pass
                room.players.clear()
                player.room_id = None
                self.rooms.pop(room.room_id, None)
                self.saved_rooms.pop(room.room_id, None)
                self.saved_worlds.pop(room.room_id, None)
                self.save_state()
                self.log("room_closed_owner_left", room=room.room_id,
                         owner=player.player_id, kicked=len(guests))
                try:
                    player.writer.close(); await player.writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
                return
            self.remember_player(player)
            for key, members in list(room.parties.items()):
                if player.player_id not in members:
                    continue
                members.remove(player.player_id)
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
            for key, trade in list(room.trades.items()):
                if player.player_id not in trade.get("players", []):
                    continue
                for member in trade.get("players", []):
                    if member != player.player_id and member in room.players:
                        await send_json(room.players[member].writer,
                                        {"type": "trade_cancel", "seq": room.sequence})
                room.trades.pop(key, None)
            duel_peer = room.duels.pop(player.player_id, "")
            if duel_peer:
                room.duels.pop(duel_peer, None)
                peer_player = room.players.get(duel_peer)
                if peer_player is not None:
                    await send_json(peer_player.writer,
                                    {"type": "duel_end", "seq": room.sequence,
                                     "loser_id": player.player_id,
                                     "loser_actor_id": player.actor_id,
                                     "winner_id": peer_player.player_id,
                                     "winner_actor_id": peer_player.actor_id})
            room.players.pop(player.player_id, None)
            await self.broadcast(room, {"type": "player_leave", "player_id": player.player_id})
            self.save_state()
            if not room.players:
                self.rooms.pop(room.room_id, None)
        try:
            player.writer.close()
            await player.writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def join(self, player: Player, room_id: str, password: str = "",
                   skip_password: bool = False) -> None:
        if not getattr(player, "actor_id", 0):
            player.actor_id = self.actor_id_for(player.player_id)
        if not hasattr(player, "appearance"):
            player.appearance = {}
        room = self.rooms.get(room_id)
        if not room:
            await send_json(player.writer, {"type": "error", "code": "room_not_found"})
            self.log("join_rejected", player_id=player.player_id, room=room_id,
                     reason="room_not_found")
            return
        if bool(getattr(player, "cheat_enabled", False)) != room.cheat_enabled:
            await send_json(player.writer, {"type": "error", "code": "cheat_mismatch"})
            self.log("join_rejected", player_id=player.player_id, room=room_id,
                     reason="cheat_mismatch", room_cheat=room.cheat_enabled,
                     player_cheat=bool(getattr(player, "cheat_enabled", False)))
            return
        if player.player_id not in room.players and len(room.players) >= MAX_ROOM_PLAYERS:
            await send_json(player.writer, {"type": "error", "code": "room_full"})
            self.log("join_rejected", player_id=player.player_id, room=room_id,
                     reason="room_full", players=len(room.players),
                     capacity=MAX_ROOM_PLAYERS)
            return
        supplied_hash = password_digest(password)
        if not skip_password and not hmac.compare_digest(room.password_hash, supplied_hash):
            await send_json(player.writer, {"type": "error", "code": "wrong_password"})
            self.log("join_rejected", player_id=player.player_id, room=room_id,
                     reason="wrong_password")
            return
        if player.room_id:
            await self.disconnect_from_room(player)
        player.room_id = room.room_id
        if not room.owner_id:
            room.owner_id = player.player_id
        resume_token = secrets.token_urlsafe(32)
        player.resume_token_hash = hashlib.sha256(
            resume_token.encode("utf-8")).hexdigest()
        room.ensure_world()
        room.players[player.player_id] = player
        self.remember_player(player)
        players = [self.player_snapshot(p) for p in room.players.values()]
        if not players and room.room_id in self.saved_rooms:
            players = list(self.saved_rooms[room.room_id])
        await send_json(player.writer, {"type": "room_joined", "room": room.room_id,
                                        "player_id": player.player_id,
                                        "resume_token": resume_token,
                                        "owner_id": room.owner_id,
                                        "cheat_enabled": room.cheat_enabled,
                                        "players": players, "mobs": list(room.mobs.values()),
                                        "drops": list(room.drops.values()),
                                        "shinwa": list(room.shinwa.values()),
                                        "clones": [dict(getattr(p, "clone", {}), player_id=p.player_id,
                                                        clone_actor_id=self.actor_id_for(p.player_id + "#clone"))
                                                   for p in room.players.values()
                                                   if getattr(p, "clone", {}).get("active")]})
        for event in list(room.pending_deliveries.get(player.player_id, {}).values()):
            await send_json(player.writer, dict(event))
        clan = self.clan_for_player(room, player.player_id)
        if clan is None and getattr(player, "clan_name", ""):
            clan = self.ensure_clan(room, player, player.clan_name)
        if clan is not None:
            await send_json(player.writer, self.clan_payload(room, clan))
        await self.broadcast(room, {"type": "player_join", "player_id": player.player_id,
                                     "actor_id": player.actor_id, "name": player.name,
                                     "appearance": dict(player.appearance)}, player.player_id)
        self.save_state()
        self.log("room_joined", room=room.room_id, player_id=player.player_id,
                 name=player.name, players=len(room.players), map=player.map_id)

    async def resume(self, player: Player, player_id: str, room_id: str,
                     resume_token: str) -> None:
        if not player_id or not room_id or not resume_token:
            await send_json(player.writer, {"type": "error", "code": "resume_fields_required"})
            return
        room = self.rooms.get(room_id)
        if room is None or room.owner_id not in room.players:
            await send_json(player.writer, {"type": "error", "code": "room_not_found"})
            return
        rows = self.saved_rooms.get(room_id, [])
        saved = None
        for row in rows:
            if row.get("player_id") == player_id:
                saved = row
                break
        if saved is None:
            await send_json(player.writer, {"type": "error", "code": "resume_not_found"})
            return
        expected = clean_text(saved.get("resume_token_hash"), 128)
        supplied = hashlib.sha256(resume_token.encode("utf-8")).hexdigest()
        if not expected or not hmac.compare_digest(expected, supplied):
            await send_json(player.writer, {"type": "error", "code": "resume_denied"})
            self.log("resume_rejected", player_id=player_id, room=room_id,
                     reason="invalid_token")
            return
        active = self.players.get(player_id)
        if active is not None and active is not player:
            await send_json(player.writer, {"type": "error", "code": "resume_active"})
            self.log("resume_rejected", player_id=player_id, room=room_id,
                     reason="active_session")
            return
        self.players.pop(player.player_id, None)
        player.player_id = player_id
        player.name = clean_text(saved.get("name"), MAX_NAME) or "player"
        player.actor_id = int(saved.get("actor_id", self.actor_id_for(player_id)))
        appearance = saved.get("appearance", {})
        player.appearance = dict(appearance) if isinstance(appearance, dict) else {}
        player.clan_name = clean_text(player.appearance.get("clan_name"), MAX_NAME)
        player.map_id = int(saved.get("map", 1))
        player.x = int(saved.get("x", 0))
        player.y = int(saved.get("y", 0))
        player.hp = int(saved.get("hp", 0))
        player.cheat_enabled = bool(saved.get("cheat_enabled", False))
        player.resume_token_hash = expected
        clone = saved.get("clone", {})
        player.clone = dict(clone) if isinstance(clone, dict) else {}
        self.players[player.player_id] = player
        await self.join(player, room_id, skip_password=True)

    async def disconnect_from_room(self, player: Player) -> None:
        room = self.rooms.get(player.room_id or "")
        if room is None:
            player.room_id = None
            return
        await self.clear_cuu_sat_for_player(room, player, "disconnect", True)
        room.players.pop(player.player_id, None)
        player.room_id = None
        await self.broadcast(room, {"type": "player_leave", "player_id": player.player_id})
        self.log("room_left", room=room.room_id, player_id=player.player_id,
                 players=len(room.players))
        if not room.players:
            self.rooms.pop(room.room_id, None)
        self.save_state()

