from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from ..config import MAX_NAME, MAX_PASSWORD, clean_text, password_digest
from ..models import Player, Room
from ..protocol import send_json

class ClanInteractionMixin:
    async def handle_clan_interaction(
            self, room: Room, player: Player, target: Player | None,
            message: dict[str, Any], base: dict[str, Any], kind: str) -> bool:
        if kind == "clan_invite":
            clan_name = clean_text(message.get("data"), MAX_NAME)
            if not clan_name:
                await send_json(player.writer, {"type": "error", "code": "clan_required"})
                return False
            clan = self.ensure_clan(room, player, clan_name)
            role = int(clan.get("members", {}).get(player.player_id, {}).get("role", 0))
            if role < 3:
                await send_json(player.writer, {"type": "error", "code": "clan_role_denied"})
                return False
            if self.clan_for_player(room, target.player_id) is not None:
                await send_json(player.writer, {"type": "error", "code": "clan_member_busy"})
                return False
            room.clan_invites[target.player_id] = player.player_id
            await send_json(target.writer, dict(base, type="clan_invite",
                                                clan_name=clan.get("name", clan_name)))
        elif kind == "clan_accept":
            if room.clan_invites.get(player.player_id) != target.player_id:
                await send_json(player.writer, {"type": "error", "code": "clan_invite_expired"})
                return False
            room.clan_invites.pop(player.player_id, None)
            clan = self.clan_for_player(room, target.player_id)
            if clan is None:
                await send_json(player.writer, {"type": "error", "code": "clan_required"})
                return False
            if self.clan_for_player(room, player.player_id) is not None:
                await send_json(player.writer, {"type": "error", "code": "clan_member_busy"})
                return False
            player.clan_name = str(clan.get("name", ""))
            clan.setdefault("members", {})[player.player_id] = {
                "name": player.name, "class_id": int(player.appearance.get("class_id", 0)),
                "level": int(player.appearance.get("level", 1)), "role": 0, "points": 0}
            await self.broadcast(room, {"type": "clan_joined", "seq": room.sequence,
                                        "joined_id": player.player_id,
                                        "joined_actor_id": player.actor_id,
                                        "name": player.name, "clan_name": player.clan_name,
                                        "relation_type": 0})
            await self.broadcast_clan(room, clan)
        elif kind == "clan_chat":
            text = clean_text(message.get("data"), 300)
            clan = self.clan_for_player(room, player.player_id)
            if clan is None:
                await send_json(player.writer, {"type": "error", "code": "clan_required"})
                return False
            for member_id in clan.get("members", {}):
                member = room.players.get(member_id)
                if member is not None:
                    await send_json(member.writer, dict(base, type="clan_chat", text=text,
                                                        clan_name=clan.get("name", "")))
        elif kind == "clan_state_request":
            clan = self.clan_for_player(room, player.player_id)
            if clan is None:
                await send_json(player.writer, {"type": "error", "code": "clan_required"})
                return False
            await send_json(player.writer, self.clan_payload(room, clan))
        elif kind == "clan_alert":
            clan = self.clan_for_player(room, player.player_id)
            if clan is None or clan.get("leader_id") != player.player_id:
                await send_json(player.writer, {"type": "error", "code": "clan_role_denied"})
                return False
            clan["alert"] = clean_text(message.get("data"), 300)
            await self.broadcast_clan(room, clan)
        elif kind == "clan_role":
            clan = self.clan_for_player(room, player.player_id)
            if clan is None or clan.get("leader_id") != player.player_id:
                await send_json(player.writer, {"type": "error", "code": "clan_role_denied"})
                return False
            members = clan.get("members", {})
            target_member = members.get(target.player_id)
            if target_member is None or target.player_id == player.player_id:
                await send_json(player.writer, {"type": "error", "code": "clan_member_not_found"})
                return False
            try: requested = int(str(message.get("data", "0")))
            except ValueError: requested = 0
            role = requested if requested in (0, 2, 3) else 0
            if role == 3 and sum(1 for member in members.values()
                                if int(member.get("role", 0)) == 3) >= 1:
                await send_json(player.writer, {"type": "error", "code": "clan_deputy_full"})
                return False
            if role == 2 and sum(1 for member in members.values()
                                if int(member.get("role", 0)) == 2) >= 5:
                await send_json(player.writer, {"type": "error", "code": "clan_elder_full"})
                return False
            target_member["role"] = role
            await self.broadcast_clan(room, clan)
        elif kind == "clan_kick":
            clan = self.clan_for_player(room, player.player_id)
            if clan is None or clan.get("leader_id") != player.player_id:
                await send_json(player.writer, {"type": "error", "code": "clan_role_denied"})
                return False
            member = clan.get("members", {}).get(target.player_id)
            if member is None or target.player_id == player.player_id:
                await send_json(player.writer, {"type": "error", "code": "clan_member_not_found"})
                return False
            fee = {3: 100000, 2: 50000, 1: 20000}.get(int(member.get("role", 0)), 10000)
            if int(clan.get("coin", 0)) < fee:
                await send_json(player.writer, {"type": "error", "code": "clan_fund_low"})
                return False
            clan["coin"] = int(clan.get("coin", 0)) - fee
            clan["members"].pop(target.player_id, None)
            target.clan_name = ""
            await send_json(target.writer, {"type": "clan_joined", "seq": room.sequence,
                                            "joined_id": target.player_id,
                                            "joined_actor_id": target.actor_id,
                                            "name": target.name, "clan_name": "",
                                            "relation_type": -1})
            await self.broadcast_clan(room, clan)
        elif kind == "clan_leave":
            clan = self.clan_for_player(room, player.player_id)
            if clan is None:
                await send_json(player.writer, {"type": "error", "code": "clan_required"})
                return False
            if clan.get("leader_id") == player.player_id:
                await send_json(player.writer, {"type": "error", "code": "clan_leader_cannot_leave"})
                return False
            clan.get("members", {}).pop(player.player_id, None)
            player.clan_name = ""
            await send_json(player.writer, {"type": "clan_joined", "seq": room.sequence,
                                            "joined_id": player.player_id,
                                            "joined_actor_id": player.actor_id,
                                            "name": player.name, "clan_name": "",
                                            "relation_type": -1})
            await self.broadcast_clan(room, clan)
        elif kind in ("clan_contribute", "clan_points"):
            clan = self.clan_for_player(room, player.player_id)
            if clan is None:
                await send_json(player.writer, {"type": "error", "code": "clan_required"})
                return False
            try: amount = max(0, min(int(str(message.get("data", "0"))), 100000000))
            except ValueError: amount = 0
            if amount <= 0:
                await send_json(player.writer, {"type": "error", "code": "clan_amount_invalid"})
                return False
            if kind == "clan_contribute":
                clan["coin"] = min(2147483647, int(clan.get("coin", 0)) + amount)
            else:
                clan["exp"] = min(2147483647, int(clan.get("exp", 0)) + amount)
                member = clan.get("members", {}).get(player.player_id, {})
                member["points"] = min(2147483647, int(member.get("points", 0)) + amount)
            await self.broadcast_clan(room, clan)
        elif kind == "clan_upgrade":
            clan = self.clan_for_player(room, player.player_id)
            if clan is None or clan.get("leader_id") != player.player_id:
                await send_json(player.writer, {"type": "error", "code": "clan_role_denied"})
                return False
            level = int(clan.get("level", 1)); exp = self.clan_exp_next(level)
            coin = self.clan_coin_up(level)
            if int(clan.get("exp", 0)) < exp or int(clan.get("coin", 0)) < coin:
                await send_json(player.writer, {"type": "error", "code": "clan_upgrade_cost"})
                return False
            clan["exp"] -= exp; clan["coin"] -= coin; clan["level"] = level + 1
            await self.broadcast_clan(room, clan)
        elif kind == "clan_item_upgrade":
            clan = self.clan_for_player(room, player.player_id)
            if clan is None or clan.get("leader_id") != player.player_id:
                await send_json(player.writer, {"type": "error", "code": "clan_role_denied"})
                return False
            item_level = int(clan.get("item_level", 0))
            prices = (1000000, 5000000, 10000000, 20000000, 30000000)
            if item_level >= len(prices): return
            if int(clan.get("level", 1)) < (item_level + 1) * 5 \
                    or int(clan.get("coin", 0)) < prices[item_level]:
                await send_json(player.writer, {"type": "error", "code": "clan_upgrade_cost"})
                return False
            clan["coin"] -= prices[item_level]; clan["item_level"] = item_level + 1
            await self.broadcast_clan(room, clan)
        elif kind == "clan_store_buy":
            clan = self.clan_for_player(room, player.player_id)
            role = int(clan.get("members", {}).get(player.player_id, {}).get("role", 0)) if clan else 0
            if clan is None or role < 3:
                await send_json(player.writer, {"type": "error", "code": "clan_role_denied"})
                return False
            parts = str(message.get("data", "")).split(",", 7)
            try:
                item, quantity, unit = int(parts[0]), int(parts[1]), int(parts[2])
                locked, upgrade, system = int(parts[3]), int(parts[4]), int(parts[5])
                expire = int(parts[6]); options = parts[7] if len(parts) > 7 else ""
            except (ValueError, IndexError):
                await send_json(player.writer, {"type": "error", "code": "clan_item_invalid"})
                return False
            quantity = max(1, min(quantity, 32767)); unit = max(0, min(unit, 2000000000))
            price = unit * quantity
            if item < 0 or item > 65535 or price > int(clan.get("coin", 0)):
                await send_json(player.writer, {"type": "error", "code": "clan_fund_low"})
                return False
            if 423 <= item <= 427 and int(clan.get("item_level", 0)) < item - 422:
                await send_json(player.writer, {"type": "error", "code": "clan_item_locked"})
                return False
            row = clan.setdefault("items", {}).setdefault(str(item), {
                "item": item, "quantity": 0, "locked": bool(locked),
                "upgrade": max(0, min(upgrade, 127)), "sys": max(0, min(system, 127)),
                "expire": expire, "options": clean_text(options, 512)})
            row["quantity"] = min(32767, int(row.get("quantity", 0)) + quantity)
            clan["coin"] = int(clan.get("coin", 0)) - price
            await self.broadcast_clan(room, clan)
        elif kind == "clan_item_send":
            clan = self.clan_for_player(room, player.player_id)
            role = int(clan.get("members", {}).get(player.player_id, {}).get("role", 0)) if clan else 0
            if clan is None or role < 3 or target.player_id not in clan.get("members", {}):
                await send_json(player.writer, {"type": "error", "code": "clan_role_denied"})
                return False
            key = clean_text(message.get("data"), 16)
            row = clan.setdefault("items", {}).get(key)
            if row is None or int(row.get("quantity", 0)) <= 0:
                await send_json(player.writer, {"type": "error", "code": "clan_item_invalid"})
                return False
            row["quantity"] = int(row.get("quantity", 0)) - 1
            delivery = (f'{int(row.get("item", key))},1,'
                        f'{1 if row.get("locked", False) else 0},{int(row.get("upgrade", 0))},'
                        f'{int(row.get("sys", 0))},{int(row.get("expire", -1))},'
                        f'{clean_text(row.get("options", ""), 512)}')
            if row["quantity"] <= 0: clan["items"].pop(key, None)
            await self.durable_delivery(
                room, target.player_id,
                {"type": "clan_item_delivery", "seq": room.sequence,
                 "player_id": player.player_id, "data": delivery}, "clan-item")
            await self.broadcast_clan(room, clan)
        elif kind == "clan_item_use":
            clan = self.clan_for_player(room, player.player_id)
            key = clean_text(message.get("data"), 16)
            row = clan.setdefault("items", {}).get(key) if clan else None
            territory = clan.setdefault("territory", {}) if clan else {}
            if row is None or int(row.get("item", -1)) != 281 \
                    or int(row.get("quantity", 0)) <= 0:
                await send_json(player.writer, {"type": "error", "code": "clan_item_invalid"})
                return False
            if int(territory.get("entries", 0)) != 0 or int(territory.get("use_card", 0)) <= 0 \
                    or bool(territory.get("active", False)):
                await send_json(player.writer, {"type": "error", "code": "clan_item_use_denied"})
                return False
            territory["entries"] = 1; territory["use_card"] = int(territory.get("use_card", 0)) - 1
            row["quantity"] = int(row.get("quantity", 0)) - 1
            if row["quantity"] <= 0: clan["items"].pop(key, None)
            await self.broadcast_clan(room, clan)
        elif kind == "clan_pet":
            clan = self.clan_for_player(room, player.player_id)
            role = int(clan.get("members", {}).get(player.player_id, {}).get("role", 0)) if clan else 0
            if clan is None or role < 3:
                await send_json(player.writer, {"type": "error", "code": "clan_role_denied"})
                return False
            parts = str(message.get("data", "")).split(",", 2)
            try: pet, deadline, cost = int(parts[0]), int(parts[1]), int(parts[2])
            except (ValueError, IndexError): pet, deadline, cost = -1, 0, 0
            pets = clan.setdefault("pets", [0, 0, 0])
            if pet < 0 or pet >= 3 or pets[pet] != 0 or cost < 0 \
                    or int(clan.get("coin", 0)) < cost:
                await send_json(player.writer, {"type": "error", "code": "clan_pet_invalid"})
                return False
            clan["coin"] -= cost; pets[pet] = deadline
            await self.broadcast_clan(room, clan)
        elif kind == "clan_pet_sync":
            clan = self.clan_for_player(room, player.player_id)
            parts = str(message.get("data", "")).split(",", 1)
            try: pet, state = int(parts[0]), int(parts[1])
            except (ValueError, IndexError): pet, state = -1, 0
            pets = clan.setdefault("pets", [0, 0, 0]) if clan else []
            if pet < 0 or pet >= len(pets) or pets[pet] == 0 or state == 0:
                await send_json(player.writer, {"type": "error", "code": "clan_pet_invalid"})
                return False
            pets[pet] = state
            await self.broadcast_clan(room, clan)
        elif kind == "clan_territory_open":
            clan = self.clan_for_player(room, player.player_id)
            if clan is None or clan.get("leader_id") != player.player_id:
                await send_json(player.writer, {"type": "error", "code": "clan_role_denied"})
                return False
            if player.player_id != room.owner_id:
                await send_json(player.writer, {"type": "error", "code": "owner_world_only"})
                return False
            territory = clan.setdefault("territory", {})
            territory.update({"active": True,
                              "deadline": int(time.time() * 1000) + 3600000,
                              "points": int(territory.get("points", 0))})
            event = {"type": "clan_territory", "seq": room.sequence,
                     "clan_name": clan.get("name", ""), "payload": dict(territory)}
            for member_id in clan.get("members", {}):
                member = room.players.get(member_id)
                if member is not None: await send_json(member.writer, event)
            await self.broadcast_clan(room, clan)
        elif kind == "clan_war_invite":
            own = self.clan_for_player(room, player.player_id)
            enemy = self.clan_for_player(room, target.player_id)
            if own is None or enemy is None or own is enemy \
                    or own.get("leader_id") != player.player_id \
                    or enemy.get("leader_id") != target.player_id:
                await send_json(player.writer, {"type": "error", "code": "clan_war_invalid"})
                return False
            enemy.setdefault("war", {})["pending_from"] = own.get("name", "")
            await send_json(target.writer, dict(base, type="clan_war_state",
                                                clan_name=own.get("name", ""),
                                                relation_type=0))
        elif kind == "clan_war_accept":
            clan = self.clan_for_player(room, player.player_id)
            if clan is None or clan.get("leader_id") != player.player_id:
                await send_json(player.writer, {"type": "error", "code": "clan_role_denied"})
                return False
            opponent_name = str(clan.setdefault("war", {}).pop("pending_from", ""))
            opponent = room.clans.get(opponent_name.casefold())
            if opponent is None:
                await send_json(player.writer, {"type": "error", "code": "clan_war_invalid"})
                return False
            deadline = int(time.time() * 1000) + 1800000
            clan["war"] = {"active": True, "opponent": opponent.get("name", ""),
                           "own_points": 0, "enemy_points": 0, "deadline": deadline}
            opponent["war"] = {"active": True, "opponent": clan.get("name", ""),
                               "own_points": 0, "enemy_points": 0, "deadline": deadline}
            for active in (clan, opponent):
                event = {"type": "clan_war_state", "seq": room.sequence,
                         "clan_name": active.get("name", ""),
                         "payload": dict(active.get("war", {})), "relation_type": 1}
                for member_id in active.get("members", {}):
                    member = room.players.get(member_id)
                    if member is not None: await send_json(member.writer, event)
                await self.broadcast_clan(room, active)
        elif kind == "clan_war_points":
            clan = self.clan_for_player(room, player.player_id)
            if clan is None or not clan.get("war", {}).get("active"):
                await send_json(player.writer, {"type": "error", "code": "clan_war_invalid"})
                return False
            try: points = max(0, min(int(str(message.get("data", "0"))), 14000))
            except ValueError: points = 0
            war = clan["war"]; war["own_points"] = min(14000, int(war.get("own_points", 0)) + points)
            opponent = room.clans.get(str(war.get("opponent", "")).casefold())
            if opponent is not None:
                opponent.setdefault("war", {})["enemy_points"] = war["own_points"]
            await self.broadcast_clan(room, clan)
        return True
