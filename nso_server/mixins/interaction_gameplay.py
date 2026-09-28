from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from ..config import MAX_NAME, MAX_PASSWORD, clean_text, password_digest
from ..models import Player, Room
from ..protocol import send_json

class GameplayInteractionMixin:
    async def handle_gameplay_interaction(
            self, room: Room, player: Player, target: Player | None,
            message: dict[str, Any], base: dict[str, Any], kind: str) -> bool:
        if kind == "mob_attack":
            if player.player_id != room.owner_id:
                await send_json(player.writer, {"type": "error", "code": "owner_world_only"})
                return False
            if target is None or target.map_id != player.map_id or target.hp <= 0:
                await send_json(player.writer, {"type": "error", "code": "mob_target_invalid"})
                return False
            parts = clean_text(message.get("data"), 96).split(",")
            try:
                mob_id = clean_text(parts[0], 32)
                mob_x, mob_y = int(parts[1]), int(parts[2])
            except (IndexError, ValueError):
                await send_json(player.writer, {"type": "error", "code": "mob_attack_invalid"})
                return False
            if abs(target.x - mob_x) > 300 or abs(target.y - mob_y) > 180:
                await send_json(player.writer, {"type": "error", "code": "mob_attack_range"})
                return False
            raw = max(0, min(int(message.get("damage", 0)), 2000000000))
            reduction = max(0, int(target.appearance.get("damage_down", 0)))
            system = max(0, min(int(message.get("skill", 0)), 3))
            resistance = int(target.appearance.get(
                "res_fire" if system == 1 else "res_ice" if system == 2 else "res_wind", 0))
            shown = max(1, raw - reduction - max(0, resistance)) if raw > 0 else 0
            drained = max(1, shown - shown * 80 // 100) if shown > 0 else 0
            target.hp = max(0, target.hp - drained)
            await self.broadcast(room, dict(base, type="mob_hit", mob_id=mob_id,
                                             target_id=target.player_id,
                                             target_actor_id=target.actor_id,
                                             damage=shown, amount=drained,
                                             hp=target.hp))
        elif kind == "mob_yen":
            if player.player_id != room.owner_id:
                await send_json(player.writer, {"type": "error", "code": "owner_world_only"})
                return False
            amount = max(0, min(int(message.get("damage", 0)), 2000000000))
            if amount <= 0:
                await send_json(player.writer, {"type": "error", "code": "mob_reward_invalid"})
                return False
            await self.durable_delivery(
                room, target.player_id,
                {"type": "mob_reward", "seq": room.sequence,
                 "player_id": player.player_id, "amount": amount},
                "mob-yen")
        elif kind == "world_packet":
            if player.player_id != room.owner_id:
                await send_json(player.writer, {"type": "error", "code": "owner_world_only"})
                return False
            try:
                command = int(message.get("damage", 999))
            except (TypeError, ValueError):
                command = 999
            payload = clean_text(message.get("data"), 32768).lower()
            if command < -128 or command > 127 or len(payload) > 32768 \
                    or len(payload) % 2 != 0 \
                    or any(ch not in "0123456789abcdef" for ch in payload):
                await send_json(player.writer, {"type": "error", "code": "world_packet_invalid"})
                return False
            await self.broadcast(room, dict(base, type="world_packet",
                                             command=command, data=payload),
                                 player.player_id)
        elif kind == "activity_reward":
            if player.player_id != room.owner_id:
                await send_json(player.writer, {"type": "error", "code": "owner_world_only"})
                return False
            payload = clean_text(message.get("data"), 1900)
            if "|" not in payload or not payload.split("|", 1)[1]:
                await send_json(player.writer, {"type": "error", "code": "activity_reward_invalid"})
                return False
            activity = clean_text(payload.split("|", 1)[0], 32)
            recipients = [member for member in room.players.values()
                          if member.player_id != room.owner_id
                          and member.map_id == player.map_id]
            for member in recipients:
                await self.durable_delivery(room, member.player_id,
                    {"type": "activity_reward", "seq": room.sequence,
                     "player_id": player.player_id, "activity": activity,
                     "data": payload}, "activity")
            self.log("activity_reward", room=room.room_id, activity=activity,
                     owner=player.player_id, recipients=len(recipients),
                     sequence=room.sequence)
        elif kind == "room_password":
            if player.player_id != room.owner_id:
                await send_json(player.writer, {"type": "error", "code": "owner_room_only"})
                return False
            password = clean_text(message.get("data"), MAX_PASSWORD)
            room.password_hash = password_digest(password)
            # Existing guests stay connected, but their old reconnect token is
            # invalid after this access-control boundary. A later re-entry must
            # use the new password and receives a fresh identity/token.
            for guest in room.players.values():
                if guest.player_id == room.owner_id:
                    continue
                guest.resume_token_hash = hashlib.sha256(
                    secrets.token_bytes(32)).hexdigest()
                self.remember_player(guest)
            self.save_state()
            await send_json(player.writer, {"type": "room_password_changed",
                                            "seq": room.sequence,
                                            "password": bool(password)})
            self.log("room_password_changed", room=room.room_id,
                     owner=player.player_id, password_protected=bool(password),
                     active_guests=max(0, len(room.players) - 1))
        elif kind == "room_kick":
            if player.player_id != room.owner_id:
                await send_json(player.writer, {"type": "error", "code": "owner_room_only"})
                return False
            if target is None or target.player_id == room.owner_id:
                await send_json(player.writer, {"type": "error", "code": "kick_target_invalid"})
                return False
            kicked_id = target.player_id
            target.resume_token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
            self.remember_player(target)
            try:
                await send_json(target.writer, {"type": "error", "code": "room_kicked"})
            except (ConnectionError, OSError):
                pass
            await self.disconnect(target)
            self.log("room_player_kicked", room=room.room_id,
                     owner=player.player_id, kicked=kicked_id)
        elif kind == "delivery_ack":
            delivery_id = clean_text(message.get("data"), 128)
            if not self.acknowledge_delivery(room, player.player_id, delivery_id):
                await send_json(player.writer, {"type": "error", "code": "delivery_not_found"})
                return False
        elif kind == "friend_invite":
            if room.friend_requests.get(player.player_id) == target.player_id:
                room.friend_requests.pop(player.player_id, None)
                for left, right in ((player, target), (target, player)):
                    friends = room.friends.setdefault(left.player_id, [])
                    if right.player_id not in friends: friends.append(right.player_id)
                    await send_json(left.writer, {"type": "friend_add", "seq": room.sequence,
                                                  "player_id": right.player_id,
                                                  "actor_id": right.actor_id,
                                                  "name": right.name, "relation_type": 1})
            else:
                room.friend_requests[target.player_id] = player.player_id
                await send_json(player.writer, {"type": "friend_add", "seq": room.sequence,
                                                "player_id": target.player_id,
                                                "actor_id": target.actor_id,
                                                "name": target.name, "relation_type": 0})
                await send_json(target.writer, dict(base, type="friend_invite"))
        elif kind == "shinwa_publish":
            raw = clean_text(message.get("data"), 4096)
            fields = raw.split(",", 7)
            try:
                slot = int(fields[0]); template = int(fields[1]); quantity = int(fields[2])
                price = int(fields[5]); expires_at = int(fields[6])
            except (ValueError, IndexError):
                await send_json(player.writer, {"type": "error", "code": "shinwa_invalid"})
                return False
            if slot < 0 or slot >= 10 or template < 0 or template > 65535 \
                    or quantity < 1 or quantity > 32767 or price < 1 \
                    or expires_at <= int(time.time() * 1000):
                await send_json(player.writer, {"type": "error", "code": "shinwa_invalid"})
                return False
            old_id = next((product for product, listing in room.shinwa.items()
                           if listing.get("seller_id") == player.player_id
                           and int(listing.get("slot", -1)) == slot), 0)
            product = old_id or room.next_shinwa_id
            if not old_id:
                room.next_shinwa_id += 1
            listing = {"item_id": product, "seller_id": player.player_id,
                       "seller_name": player.name, "name": player.name,
                       "slot": slot, "template": template, "quantity": quantity,
                       "data": raw, "expires_at": expires_at}
            room.shinwa[product] = listing
            await self.broadcast(room, {"type": "shinwa_listing", "seq": room.sequence,
                                         **listing})
        elif kind == "shinwa_buy":
            try: product = int(str(message.get("data", "0")))
            except ValueError: product = 0
            listing = room.shinwa.get(product)
            if listing is None or int(listing.get("expires_at", 0)) <= int(time.time() * 1000):
                room.shinwa.pop(product, None)
                await send_json(player.writer, {"type": "error", "code": "shinwa_sold"})
                return False
            if listing.get("seller_id") == player.player_id:
                await send_json(player.writer, {"type": "error", "code": "shinwa_own_item"})
                return False
            room.shinwa.pop(product, None)
            transaction = "shinwa-" + secrets.token_hex(8)
            sold = {"type": "shinwa_sold", "seq": room.sequence,
                    "transaction": transaction, "buyer_id": player.player_id,
                    "buyer_name": player.name, **listing}
            seller_id = str(listing.get("seller_id", ""))
            await self.broadcast_except(
                room, {"type": "shinwa_removed", "seq": room.sequence,
                       "item_id": product, "seller_id": seller_id},
                {player.player_id, seller_id})
            await self.durable_delivery(room, player.player_id, sold,
                                        transaction + "-buyer")
            await self.durable_delivery(room, seller_id, sold,
                                        transaction + "-seller")
        elif kind == "shinwa_remove":
            try: product = int(str(message.get("data", "0")))
            except ValueError: product = 0
            listing = room.shinwa.get(product)
            if listing is not None and listing.get("seller_id") == player.player_id:
                room.shinwa.pop(product, None)
                await self.broadcast(room, {"type": "shinwa_removed", "seq": room.sequence,
                                             "item_id": product,
                                             "seller_id": player.player_id})
        elif kind == "vxmm_bet":
            try:
                category, amount = [int(value) for value in str(message.get("data", "")).split(",", 1)]
            except (ValueError, TypeError):
                category, amount = -1, 0
            maximum = 1000000 if category == 0 else 50000000
            if category not in (0, 1) or amount < 10000 or amount > maximum:
                await send_json(player.writer, {"type": "error", "code": "vxmm_invalid_bet"})
                return False
            room.lucky_bets.setdefault(category, {})[player.player_id] = amount
            await self.broadcast(room, {"type": "vxmm_bet", "seq": room.sequence,
                                         "player_id": player.player_id, "name": player.name,
                                         "category": category, "amount": amount})
        elif kind == "vxmm_result":
            if player.player_id != room.owner_id:
                await send_json(player.writer, {"type": "error", "code": "owner_world_only"})
                return False
            parts = str(message.get("data", "")).split(",", 4)
            try:
                category, amount = int(parts[0]), int(parts[2])
                winner_id, winner_bet, winner_name = parts[1], int(parts[3]), parts[4]
            except (ValueError, IndexError):
                await send_json(player.writer, {"type": "error", "code": "vxmm_invalid_result"})
                return False
            if category not in (0, 1) or amount < 0 \
                    or (winner_id and winner_id not in room.players):
                await send_json(player.writer, {"type": "error", "code": "vxmm_invalid_result"})
                return False
            room.lucky_bets[category] = {}
            await self.broadcast(room, {"type": "vxmm_result", "seq": room.sequence,
                                         "category": category, "target_id": winner_id,
                                         "name": clean_text(winner_name, MAX_NAME),
                                         "amount": amount, "data": str(winner_bet)})
        elif kind == "dungeon_finish":
            if player.player_id != room.owner_id:
                await send_json(player.writer, {"type": "error", "code": "owner_world_only"})
                return False
            parts = str(message.get("data", "")).split(",", 2)
            try:
                level, points, reward = int(parts[0]), int(parts[1]), int(parts[2])
            except (ValueError, IndexError):
                await send_json(player.writer, {"type": "error", "code": "dungeon_result_invalid"})
                return False
            if level < 0 or level > 5 or points < 0 or reward < 0:
                await send_json(player.writer, {"type": "error", "code": "dungeon_result_invalid"})
                return False
            await self.broadcast(room, {"type": "dungeon_finish", "seq": room.sequence,
                                         "category": level, "amount": points,
                                         "quantity": reward, "owner_id": room.owner_id},
                                 player.player_id)
        elif kind == "rank_report":
            raw = str(message.get("data", ""))
            split = raw.find(",")
            board = clean_text(raw[:split] if split >= 0 else "", 64)
            try: score = max(0, min(int(raw[split + 1:]), 2147483647))
            except ValueError: score = -1
            if not board or score < 0:
                await send_json(player.writer, {"type": "error", "code": "rank_invalid"})
                return False
            room.rankings.setdefault(board, {})[player.player_id] = score
            ordered = sorted(room.rankings[board].items(),
                             key=lambda row: (-row[1], row[0]))[:12]
            rows = []
            for participant_id, points in ordered:
                participant = room.players.get(participant_id)
                if participant is not None:
                    safe_name = participant.name.replace("|", " ").replace(":", " ").replace(";", " ")
                    rows.append(f"{participant_id}:{safe_name}:{points}")
            await self.broadcast(room, {"type": "rank_state", "seq": room.sequence,
                                         "data": board + "|" + ";".join(rows)})
        elif kind == "friend_remove":
            for left, right in ((player, target), (target, player)):
                friends = room.friends.get(left.player_id, [])
                if right.player_id in friends: friends.remove(right.player_id)
            await send_json(player.writer, {"type": "friend_remove", "seq": room.sequence,
                                            "name": target.name})
        return True
