"""Protocol command dispatcher."""

from __future__ import annotations

import time
from typing import Any

from ..config import (MAX_NAME, MAX_PASSWORD, MAX_ROOM, MAX_ROOM_DROPS,
                      OWNER_RESPONSE_TIMEOUT, clean_text, password_digest)
from ..models import Player, Room
from ..protocol import send_json

class CommandsMixin:
    async def command(self, player: Player, message: dict[str, Any]) -> None:
        command = clean_text(message.get("cmd"), 24).lower()
        self.log_command(player, command, message)
        if command == "hello":
            player.name = clean_text(message.get("name"), MAX_NAME) or "player"
            await send_json(player.writer, {"type": "hello_ok", "player_id": player.player_id, "name": player.name})
            return
        if command == "create":
            if player.room_id:
                await send_json(player.writer, {"type": "error", "code": "already_in_room"})
                return
            room_id = clean_text(message.get("room"), MAX_ROOM).upper() or self.new_room_id()
            if room_id in self.rooms:
                await send_json(player.writer, {"type": "error", "code": "room_exists"})
                return
            password = clean_text(message.get("password"), MAX_PASSWORD)
            player.cheat_enabled = bool(message.get("cheat_enabled", False))
            self.rooms[room_id] = Room(room_id, owner_id=player.player_id,
                                       cheat_enabled=player.cheat_enabled,
                                       password_hash=password_digest(password))
            self.log("room_created", room=room_id, owner=player.player_id,
                     password_protected=bool(password))
            await self.join(player, room_id, password)
            return
        if command == "join":
            player.cheat_enabled = bool(message.get("cheat_enabled", False))
            await self.join(player, clean_text(message.get("room"), MAX_ROOM).upper(),
                            clean_text(message.get("password"), MAX_PASSWORD))
            return
        if command == "resume":
            await self.resume(player, clean_text(message.get("player_id"), 32),
                              clean_text(message.get("room"), MAX_ROOM).upper(),
                              clean_text(message.get("resume_token"), 128))
            return
        if command == "disconnect":
            await self.disconnect(player)
            player.writer.close()
            return
        if command == "leave":
            await self.disconnect_from_room(player)
            return
        if command == "ping":
            now = time.monotonic()
            owner_responsive = True
            owner_lag_ms = 0
            room = self.rooms.get(player.room_id or "")
            if room is not None and player.player_id != room.owner_id:
                owner = room.players.get(room.owner_id)
                owner_lag = OWNER_RESPONSE_TIMEOUT + 1.0 if owner is None else max(0.0, now - owner.last_seen)
                owner_responsive = owner is not None and owner_lag <= OWNER_RESPONSE_TIMEOUT
                owner_lag_ms = int(owner_lag * 1000.0)
            await send_json(player.writer, {"type": "pong", "time": int(time.time() * 1000),
                                            "owner_responsive": owner_responsive,
                                            "owner_lag_ms": owner_lag_ms})
            return
        if not player.room_id:
            await send_json(player.writer, {"type": "error", "code": "join_room_first"})
            return
        room = self.rooms[player.room_id]
        room.ensure_world()
        if command == "snapshot":
            self.prune_expired_drops(room)
            players = [self.player_snapshot(p) for p in room.players.values()]
            await send_json(player.writer, {"type": "snapshot", "room": room.room_id,
                                            "seq": room.sequence, "players": players,
                                            "owner_id": room.owner_id,
                                            "mobs": list(room.mobs.values()),
                                            "drops": list(room.drops.values()),
                                            "shinwa": list(room.shinwa.values()),
                                            "clones": [dict(getattr(p, "clone", {}), player_id=p.player_id,
                                                            clone_actor_id=self.actor_id_for(p.player_id + "#clone"))
                                                       for p in room.players.values()
                                                       if getattr(p, "clone", {}).get("active")]})
            return
        if command == "private_chat":
            target = clean_text(message.get("target"), MAX_NAME)
            text = clean_text(message.get("text"), 512)
            recipient = next((candidate for candidate in room.players.values()
                              if candidate.name == target), None)
            if recipient is None:
                await send_json(player.writer, {"type": "error", "code": "player_not_found"})
                return
            room.sequence += 1
            await send_json(recipient.writer, {"type": "private_chat", "seq": room.sequence,
                                               "player_id": player.player_id,
                                               "actor_id": player.actor_id,
                                               "name": player.name, "text": text})
            self.log("private_chat", room=room.room_id, player_id=player.player_id,
                     target=target, text=text, sequence=room.sequence)
            return
        if command == "interaction":
            await self.interaction(room, player, message)
            return
        if command == "clone":
            clone = self.clean_clone(message, player)
            player.clone = clone
            room.sequence += 1
            await self.broadcast(room, {"type": "clone_state", "seq": room.sequence,
                                         "player_id": player.player_id,
                                         "clone_actor_id": self.actor_id_for(player.player_id + "#clone"),
                                         "clone": dict(clone)}, player.player_id)
            self.save_state()
            self.log("clone_state", room=room.room_id, player_id=player.player_id,
                     active=clone.get("active", False), map=clone.get("map", player.map_id),
                     x=clone.get("x", 0), y=clone.get("y", 0), sequence=room.sequence)
            return
        if command == "world_sync":
            if player.player_id != room.owner_id:
                await send_json(player.writer, {"type": "error", "code": "owner_world_only"})
                return
            room.sequence += 1
            await self.broadcast(room, {"type": "world_sync", "seq": room.sequence,
                                         "owner_id": room.owner_id,
                                         "map": player.map_id,
                                         "world_epoch": int(message.get("world_epoch", 0)),
                                         "payload": clean_text(message.get("payload"), 65535)},
                                 player.player_id)
            if self.trace_hot_path:
                self.log("world_sync", room=room.room_id, owner=room.owner_id,
                         bytes=len(str(message.get("payload", ""))), sequence=room.sequence)
            return
        if command == "appearance":
            selected_cheat = bool(message.get("cheat_enabled", False))
            if selected_cheat != room.cheat_enabled:
                await send_json(player.writer, {"type": "error", "code": "cheat_mismatch"})
                self.log("appearance_rejected", room=room.room_id,
                         player_id=player.player_id, reason="cheat_mismatch",
                         room_cheat=room.cheat_enabled, player_cheat=selected_cheat)
                await self.disconnect(player)
                return
            player.cheat_enabled = selected_cheat
            appearance = self.clean_appearance(message)
            if not appearance.get("name"):
                await send_json(player.writer, {"type": "error", "code": "character_name_required"})
                return
            if player.player_id == room.owner_id:
                raw_reserved = str(message.get("reserved_names") or "")[:8192].replace("\r", "\n")
                room.reserved_names = [clean_text(name, MAX_NAME) for name in raw_reserved.split("\n")
                                       if clean_text(name, MAX_NAME)]
            requested_name = appearance["name"]
            assigned_name = self.unique_room_name(room, player, requested_name)
            appearance["name"] = assigned_name
            if assigned_name != requested_name:
                await send_json(player.writer, {"type": "name_assigned", "seq": room.sequence,
                                                "name": assigned_name})
            player.appearance = appearance
            player.name = assigned_name
            requested_clan_name = clean_text(appearance.get("clan_name"), MAX_NAME)
            player.clan_name = requested_clan_name
            clan = self.ensure_clan(room, player, player.clan_name) if player.clan_name else None
            if clan is not None:
                player.clan_name = str(clan.get("name", player.clan_name))
                appearance["clan_name"] = player.clan_name
                if player.clan_name != requested_clan_name:
                    await send_json(player.writer, {"type": "clan_name_assigned",
                                                    "seq": room.sequence,
                                                    "clan_name": player.clan_name})
            room.sequence += 1
            await self.broadcast(room, {"type": "player_appearance", "seq": room.sequence,
                                         "player_id": player.player_id,
                                         "actor_id": player.actor_id,
                                         "appearance": dict(appearance)}, player.player_id)
            self.save_state()
            self.log("player_appearance", room=room.room_id, player_id=player.player_id,
                     actor_id=player.actor_id, name=player.name,
                     head=appearance["head"], weapon=appearance["weapon"],
                     body=appearance["body"], leg=appearance["leg"],
                     pet_template=appearance["pet_template"], sequence=room.sequence)
            if clan is not None:
                await self.broadcast_clan(room, clan)
            return
        if command == "map":
            next_map = int(message.get("map", player.map_id))
            if next_map != player.map_id:
                await self.clear_cuu_sat_for_player(room, player,
                                                    "map_transition", True)
            player.map_id = next_map
            player.x = int(message.get("x", player.x))
            player.y = int(message.get("y", player.y))
            room.sequence += 1
            await self.broadcast(room, {"type": "map_transition", "seq": room.sequence,
                                         "player_id": player.player_id, "actor_id": player.actor_id,
                                         "map": player.map_id,
                                         "x": player.x, "y": player.y}, player.player_id)
            self.save_state()
            self.log("map_transition", room=room.room_id, player_id=player.player_id,
                     map=player.map_id, x=player.x, y=player.y, sequence=room.sequence)
            return
        if command == "attack":
            mob_id = clean_text(message.get("mob_id"), 32)
            if not mob_id:
                await send_json(player.writer, {"type": "error", "code": "mob_id_required"})
                return
            mob_key = f"{player.map_id}:{mob_id}"
            mob = room.mobs.get(mob_key)
            reported_max = max(1, min(int(message.get("max_hp", 100)), 2000000000))
            reported_hp = max(0, min(int(message.get("hp", reported_max)), reported_max))
            damage = max(0, min(int(message.get("damage", 0)), 1000000))
            # The room creator owns world respawn cadence. A zero-damage,
            # positive-HP projection revives the exact dead/missing mob row;
            # guests cannot forge this transition.
            if damage == 0 and reported_hp > 0 \
                    and player.player_id == room.owner_id \
                    and (mob is None or not mob.get("alive", False)):
                if mob is None:
                    mob = {"mob_id": mob_id, "map": player.map_id,
                           "x": player.x, "y": player.y}
                    room.mobs[mob_key] = mob
                mob["template"] = max(0, min(
                    int(message.get("drop_template", mob.get("template", 0))), 65535))
                mob["level_boss"] = max(0, min(
                    int(message.get("level_boss", mob.get("level_boss", 0))), 255))
                mob["max_hp"] = reported_max
                mob["hp"] = reported_hp
                mob["alive"] = True
                room.sequence += 1
                await self.broadcast(room, {"type": "mob_state", "seq": room.sequence,
                                             "mob": dict(mob), "attacker": "",
                                             "attacker_actor_id": 0, "skill": -1})
                self.save_state()
                self.log_hot_path("mob_respawn", room=room.room_id,
                                  player_id=player.player_id, map=player.map_id,
                                  mob_id=mob_id, hp=reported_hp,
                                  sequence=room.sequence)
                return
            if mob is None:
                mob = {"mob_id": mob_id, "template": int(message.get("drop_template", 1)),
                       "map": player.map_id, "x": player.x, "y": player.y,
                       "hp": reported_max, "max_hp": reported_max,
                       "alive": reported_max > 0}
                room.mobs[mob_key] = mob
            elif not mob.get("alive", False):
                if reported_hp > 0:
                    await send_json(player.writer, {"type": "error", "code": "mob_not_found"})
                else:
                    await send_json(player.writer, {"type": "mob_state", "seq": room.sequence,
                                                    "mob": dict(mob), "attacker": player.player_id})
                return
            was_alive = bool(mob.get("alive", False))
            mob["max_hp"] = reported_max
            mob["template"] = max(0, min(int(message.get("drop_template", mob.get("template", 0))), 65535))
            mob["level_boss"] = max(0, min(int(message.get("level_boss", mob.get("level_boss", 0))), 255))
            mob["hp"] = max(0, int(mob["hp"]) - damage) if was_alive else reported_hp
            mob["alive"] = mob["hp"] > 0
            room.sequence += 1
            skill = max(-1, min(int(message.get("skill", -1)), 127))
            await self.broadcast(room, {"type": "mob_state", "seq": room.sequence,
                                         "mob": dict(mob), "attacker": player.player_id,
                                         "attacker_actor_id": player.actor_id,
                                         "skill": skill})
            party_exp = max(0, min(int(message.get("party_exp", 0)), 2147483647))
            members = next((members for members in room.parties.values()
                            if player.player_id in members), [])
            if was_alive and not mob["alive"] and members:
                for member_id in members:
                    member = room.players.get(member_id)
                    if member is None or member is player or member.map_id != player.map_id \
                            or member.hp <= 0:
                        continue
                    await send_json(member.writer, {"type": "party_kill",
                                                    "seq": room.sequence,
                                                    "player_id": player.player_id,
                                                    "template": mob["template"],
                                                    "level_boss": mob["level_boss"]})
            if party_exp > 0 and members:
                attacker_level = int(player.appearance.get("level", 1))
                for member_id in members:
                    member = room.players.get(member_id)
                    if member is None or member is player or member.map_id != player.map_id \
                            or member.hp <= 0:
                        continue
                    member_level = int(member.appearance.get("level", 1))
                    if abs(attacker_level - member_level) <= 10:
                        await send_json(member.writer, {"type": "party_reward",
                                                        "seq": room.sequence,
                                                        "player_id": player.player_id,
                                                        "amount": party_exp})
            self.save_state()
            self.log_hot_path("combat", room=room.room_id,
                              player_id=player.player_id, map=player.map_id,
                              mob_id=mob_id, damage=damage, hp=mob["hp"],
                              alive=mob["alive"], sequence=room.sequence)
            return
        if command == "drop":
            self.prune_expired_drops(room)
            if len(room.drops) >= MAX_ROOM_DROPS:
                await send_json(player.writer, {"type": "error", "code": "too_many_drops"})
                return
            item_id = max(0, min(int(message.get("item_id", 0)), 65535))
            template = max(0, min(int(message.get("template", 0)), 65535))
            map_id = int(message.get("map", player.map_id))
            if map_id != player.map_id:
                await send_json(player.writer, {"type": "error", "code": "drop_wrong_map"})
                return
            drop_id = f"{player.player_id}-{item_id}"
            drop = {"drop_id": drop_id, "template": template, "map": map_id,
                    "x": int(message.get("x", player.x)),
                    "y": int(message.get("y", player.y)), "owner": player.player_id,
                    "quantity": max(1, min(int(message.get("quantity", 1)), 32767)),
                    "locked": bool(message.get("locked", False)),
                    "upgrade": max(0, min(int(message.get("upgrade", 0)), 255)),
                    "sys_up": max(0, min(int(message.get("sys_up", 0)), 255)),
                    "expire": int(message.get("expire", -1)),
                    "data": clean_text(message.get("data"), 1024),
                    "expires_at": int(time.time() * 1000) + 30000}
            room.drops[drop_id] = drop
            room.sequence += 1
            await self.broadcast(room, {"type": "drop_spawn", "seq": room.sequence,
                                         "drop": dict(drop)})
            self.save_state()
            self.log_hot_path("drop_spawn", room=room.room_id,
                              player_id=player.player_id, drop_id=drop_id,
                              template=template, map=map_id,
                              sequence=room.sequence)
            return
        if command == "pickup":
            drop_id = clean_text(message.get("drop_id"), 32)
            drop = room.drops.get(drop_id)
            if drop is None:
                await send_json(player.writer, {"type": "error", "code": "drop_not_found"})
                return
            if int(drop.get("expires_at", 0)) > 0 and int(time.time() * 1000) >= int(drop["expires_at"]):
                room.drops.pop(drop_id, None)
                room.sequence += 1
                await self.broadcast(room, {"type": "drop_taken", "seq": room.sequence,
                                             "drop_id": drop_id, "player_id": ""})
                await send_json(player.writer, {"type": "error", "code": "drop_expired"})
                self.save_state()
                return
            if int(drop.get("map", -1)) != player.map_id:
                await send_json(player.writer, {"type": "error", "code": "drop_wrong_map"})
                return
            if abs(int(drop.get("x", 0)) - player.x) >= 150 \
                    or abs(int(drop.get("y", 0)) - player.y) >= 150:
                await send_json(player.writer, {"type": "error", "code": "drop_too_far"})
                return
            room.drops.pop(drop_id, None)
            room.sequence += 1
            await self.broadcast(room, {"type": "drop_taken", "seq": room.sequence,
                                         "drop_id": drop_id, "player_id": player.player_id})
            self.save_state()
            self.log("drop_taken", room=room.room_id, player_id=player.player_id,
                     drop_id=drop_id, sequence=room.sequence)
            return
        if command == "state":
            next_map = int(message.get("map", player.map_id))
            if next_map != player.map_id:
                await self.clear_cuu_sat_for_player(room, player,
                                                    "map_transition", True)
            next_x = int(message.get("x", player.x))
            next_y = int(message.get("y", player.y))
            next_hp = int(message.get("hp", player.hp))
            state = (next_map, next_x, next_y, next_hp)
            if player.last_state == state:
                return
            player.map_id = next_map
            player.x = next_x
            player.y = next_y
            player.hp = next_hp
            player.last_state = state
            room.sequence += 1
            await self.broadcast(room, {"type": "player_state", "seq": room.sequence, "player_id": player.player_id,
                                         "actor_id": player.actor_id,
                                         "map": player.map_id, "x": player.x, "y": player.y, "hp": player.hp}, player.player_id)
            self.save_state()
            if self.trace_hot_path:
                self.log("player_state", room=room.room_id, player_id=player.player_id,
                         map=player.map_id, x=player.x, y=player.y, hp=player.hp,
                         sequence=room.sequence)
            return
        if command in ("event", "chat"):
            body = clean_text(message.get("text"), 512) if command == "chat" else message.get("payload", {})
            room.sequence += 1
            await self.broadcast(room, {"type": command, "seq": room.sequence, "player_id": player.player_id,
                                         "text": body} if command == "chat" else {"type": "event", "seq": room.sequence,
                                         "player_id": player.player_id, "payload": body}, player.player_id)
            self.save_state()
            self.log(command, room=room.room_id, player_id=player.player_id,
                     text=body if command == "chat" else "<event>", sequence=room.sequence)
            return
        await send_json(player.writer, {"type": "error", "code": "unknown_command"})

