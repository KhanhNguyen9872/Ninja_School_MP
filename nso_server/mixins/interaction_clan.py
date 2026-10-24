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
        current_clan = self.clan_for_player(room, player.player_id)
        if current_clan is not None: self.refresh_clan_territory(current_clan)
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
        elif kind == "clan_seed":
            clan = self.clan_for_player(room, player.player_id)
            if clan is None:
                await send_json(player.writer, {"type": "error", "code": "clan_required"})
                return False
            # A room clan starts as an empty level-1 record because the client
            # historically sent only its display name. Accept one durable
            # local snapshot before anyone has changed the shared record.
            if (int(clan.get("level", 1)) != 1 or int(clan.get("exp", 0)) != 0
                    or int(clan.get("coin", 0)) != 0
                    or int(clan.get("item_level", 0)) != 0
                    or clan.get("items")):
                return True
            parts = str(message.get("data", "")).split("#", 4)
            if len(parts) != 5:
                await send_json(player.writer, {"type": "error", "code": "clan_seed_invalid"})
                return False
            try:
                level, exp, coin, item_level = (max(1, int(parts[0])), max(0, int(parts[1])),
                                                max(0, int(parts[2])), max(0, int(parts[3])))
            except (TypeError, ValueError):
                await send_json(player.writer, {"type": "error", "code": "clan_seed_invalid"})
                return False
            items: dict[str, dict[str, Any]] = {}
            for raw in parts[4].split(";") if parts[4] else []:
                fields = raw.split(",", 6)
                if len(fields) < 7:
                    continue
                try:
                    item, quantity = int(fields[0]), max(0, min(int(fields[1]), 32767))
                    if item < 0 or quantity <= 0:
                        continue
                    items[str(item)] = {"item": item, "quantity": quantity,
                                        "locked": bool(int(fields[2])),
                                        "upgrade": max(0, min(int(fields[3]), 127)),
                                        "sys": max(0, min(int(fields[4]), 127)),
                                        "expire": int(fields[5]), "options": clean_text(fields[6], 512)}
                except (TypeError, ValueError):
                    continue
            clan["level"] = min(level, 160); clan["exp"] = min(exp, 2147483647)
            clan["coin"] = min(coin, 2147483647); clan["item_level"] = min(item_level, 127)
            clan["items"] = items
            await self.broadcast_clan(room, clan)
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
        elif kind == "clan_black_saturday_claim":
            clan = self.clan_for_player(room, player.player_id)
            if clan is None:
                await send_json(player.writer, {"type": "error", "code": "clan_required"}); return False
            member = clan.get("members", {}).get(player.player_id, {})
            if clan.get("leader_id") != player.player_id or int(member.get("role", 0)) != 4:
                await send_json(player.writer, {"type": "error", "code": "clan_role_denied"}); return False
            parts = str(message.get("data", "")).split(",")
            try: week, rank = (int(parts[0]), int(parts[1])) if len(parts) == 2 else (0, 0)
            except (ValueError, TypeError): week, rank = 0, 0
            local_day = int((time.time() + 7 * 3600) // 86400)
            latest_week = local_day - ((local_day - 2) % 7)
            minute = int((time.time() + 7 * 3600) // 60) % 1440
            if local_day == latest_week and minute < 22 * 60 + 30: latest_week -= 7
            if week <= 0 or (week - 2) % 7 or week > latest_week or not 1 <= rank <= 10:
                await send_json(player.writer, {"type": "error", "code": "black_saturday_claim_invalid"}); return False
            # The offline event board remains the intentional owner-derived
            # adapter. Only the settled rank is supplied; never trust a payout
            # amount. Identity/dedupe belong to this actual clan and week,
            # not to the claimant, request ID, clan display alias or leader.
            awards = clan.setdefault("black_saturday_rewards", {})
            award = awards.get(str(week))
            if award is not None and int(award.get("rank", 0)) != rank:
                await send_json(player.writer, {"type": "error", "code": "black_saturday_claim_invalid"}); return False
            if award is None:
                amount = {1: 100000000, 2: 70000000, 3: 50000000}.get(rank, 20000000)
                if int(clan.get("coin", 0)) + amount > 2000000000:
                    await send_json(player.writer, {"type": "error", "code": "black_saturday_fund_limit"}); return False
                award = {"rank": rank, "amount": amount, "claims": {}}
                awards[str(week)] = award
                clan["coin"] = int(clan.get("coin", 0)) + amount
            decisions = award.setdefault("claims", {})
            decision = decisions.get(player.player_id)
            if decision is None:
                decision = {"type": "activity_state", "phase": "black_saturday_claim",
                            "category": 1, "amount": int(award["amount"]),
                            "data": str(week) + "," + str(rank),
                            "player_id": player.player_id, "clan_name": str(clan.get("name", "")),
                            "room": room.room_id, "owner_id": room.owner_id,
                            "delivery_id": "black-saturday-" + secrets.token_hex(12)}
                decisions[player.player_id] = decision
            # Stage coin, the clan/week award and its decision together before
            # the first await; failed sockets leave a durable obligation.
            room.pending_deliveries.setdefault(player.player_id, {})[decision["delivery_id"]] = dict(decision)
            self.save_state(); await self.flush_state()
            try: await send_json(player.writer, dict(decision))
            except (ConnectionError, OSError): pass
            await self.broadcast_clan(room, clan)
        elif kind == "clan_contribute" and "," in str(message.get("data", "")):
            raw = str(message.get("data", "")); tx, value = raw.split(",", 1)
            try: amount = int(value)
            except ValueError: amount = 0
            if (not tx.startswith("cc-") or len(tx) > 120 or any(c in tx for c in "|\n\r")
                    or not 1000 <= amount <= 100000000):
                await send_json(player.writer, {"type": "error", "code": "clan_amount_invalid"}); return False
            decision = room.clan_contributions.get(tx)
            if decision is not None and (decision.get("player_id") != player.player_id or decision.get("amount") != amount):
                await send_json(player.writer, {"type": "error", "code": "clan_amount_invalid"}); return False
            clan = self.clan_for_player(room, player.player_id)
            if decision is None:
                accepted = clan is not None and int(clan.get("coin", 0)) + amount <= 2000000000
                decision = {"type": "activity_state", "phase": "clan_contribution", "data": tx,
                            "amount": amount, "category": int(accepted), "player_id": player.player_id,
                            "delivery_id": "contribution-" + secrets.token_hex(12)}
                room.clan_contributions[tx] = decision
                if accepted: clan["coin"] = int(clan.get("coin", 0)) + amount
                room.pending_deliveries.setdefault(player.player_id, {})[decision["delivery_id"]] = dict(decision)
                self.save_state(); await self.flush_state()
            try: await send_json(player.writer, dict(decision))
            except (ConnectionError, OSError): pass
            if clan is not None: await self.broadcast_clan(room, clan)
        elif kind in ("clan_contribute", "clan_points"):
            clan = self.clan_for_player(room, player.player_id)
            if clan is None:
                await send_json(player.writer, {"type": "error", "code": "clan_required"})
                return False
            if kind == "clan_points" and player.player_id != room.owner_id:
                await send_json(player.writer, {"type": "error", "code": "owner_world_only"})
                return False
            try: amount = max(0, min(int(str(message.get("data", "0"))), 100000000))
            except ValueError: amount = 0
            if amount <= 0:
                await send_json(player.writer, {"type": "error", "code": "clan_amount_invalid"})
                return False
            if kind == "clan_contribute":
                if int(clan.get("coin", 0)) + amount > 2000000000:
                    await send_json(player.writer, {"type": "error", "code": "clan_fund_limit"}); return False
                clan["coin"] = int(clan.get("coin", 0)) + amount
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
            # Clients can share an old warehouse snapshot. Admission must be
            # atomic here too: never charge the full price for a clipped stack.
            old_quantity = int(clan.get("items", {}).get(str(item), {}).get("quantity", 0))
            if quantity < 1 or quantity > 32767 or old_quantity + quantity > 32767:
                await send_json(player.writer, {"type": "error", "code": "clan_item_quantity_limit"})
                return False
            unit = max(0, min(unit, 2000000000))
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
            row["quantity"] = int(row.get("quantity", 0)) + quantity
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
        elif kind == "clan_territory_invite":
            clan = self.clan_for_player(room, player.player_id)
            role = int(clan.get("members", {}).get(player.player_id, {}).get("role", 0)) if clan else 0
            territory = self.refresh_clan_territory(clan) if clan else {}
            if (role < 3 or not territory.get("active") or territory.get("started") or target is None
                    or target.player_id not in clan.get("members", {})):
                await send_json(player.writer, {"type": "error", "code": "clan_territory_denied"}); return False
            guests = territory.setdefault("guests", [])
            if target.player_id not in guests: guests.append(target.player_id)
            await send_json(target.writer, {"type": "clan_territory", "category": 3,
                                            "data": territory.get("id", ""), "name": player.name})
            await self.broadcast_clan(room, clan)
        elif kind in ("clan_territory_sync", "clan_territory_close"):
            clan = self.clan_for_player(room, player.player_id)
            territory = self.refresh_clan_territory(clan) if clan else {}
            parts = str(message.get("data", "")).split(",", 2)
            if (player.player_id != room.owner_id or not parts or parts[0] != territory.get("id")):
                await send_json(player.writer, {"type": "error", "code": "clan_territory_denied"}); return False
            if kind == "clan_territory_close":
                territory["active"] = False
            elif territory.get("active"):
                try: started, deadline = int(parts[1]) != 0, int(parts[2])
                except (ValueError, IndexError): return False
                now = int(time.time() * 1000)
                if not now <= deadline <= now + 3600000: return False
                territory["started"] = bool(territory.get("started")) or started
                territory["deadline"] = deadline
            if not territory.get("active"):
                for member_id, pending in list(room.pending_deliveries.items()):
                    for delivery, event in list(pending.items()):
                        if event.get("type") == "clan_territory" and event.get("data") == territory.get("id"):
                            pending.pop(delivery, None)
                    if not pending: room.pending_deliveries.pop(member_id, None)
            await self.broadcast_clan(room, clan)
        elif kind == "clan_territory_open":
            clan = self.clan_for_player(room, player.player_id)
            if clan is None:
                await send_json(player.writer, {"type": "error", "code": "clan_required"}); return False
            territory = self.refresh_clan_territory(clan)
            role = int(clan.get("members", {}).get(player.player_id, {}).get("role", 0))
            now = int(time.time() * 1000)
            fresh = not territory.get("active")
            if fresh:
                if role < 3 or player.player_id != room.owner_id:
                    await send_json(player.writer, {"type": "error", "code": "clan_role_denied"}); return False
                if int(territory.get("entries", 0)) <= 0:
                    await send_json(player.writer, {"type": "error", "code": "clan_territory_no_entries"}); return False
                territory.update(active=True, started=False, id=secrets.token_hex(12),
                                 deadline=now + 600000, points=0, members=[], guests=[],
                                 creator=player.player_id, entries=int(territory.get("entries", 0)) - 1)
            members = territory.setdefault("members", [])
            if ((role < 3 and player.player_id not in territory.get("guests", []))
                    or (territory.get("started") and player.player_id not in members)):
                await send_json(player.writer, {"type": "error", "code": "clan_territory_denied"}); return False
            if player.player_id not in members: members.append(player.player_id)
            delivery = "territory-" + territory["id"] + "-" + player.player_id
            event = {"type": "clan_territory", "seq": room.sequence, "category": 1,
                     "clan_name": clan.get("name", ""), "data": territory["id"],
                     "owner_id": territory.get("creator", ""), "payload": dict(territory),
                     "delivery_id": delivery}
            room.pending_deliveries.setdefault(player.player_id, {})[delivery] = event
            self.save_state(); await self.flush_state()
            try: await send_json(player.writer, event)
            except (ConnectionError, OSError): pass
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
