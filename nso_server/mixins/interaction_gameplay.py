from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from ..config import MAX_NAME, MAX_PASSWORD, MAX_YEN_TRANSFER, clean_text, password_digest
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
            if target is None or target.map_id != player.map_id \
                    or target.zone_id != player.zone_id or target.hp <= 0:
                await send_json(player.writer, {"type": "error", "code": "mob_target_invalid"})
                return False
            parts = clean_text(message.get("data"), 96).split(",")
            try:
                mob_id = clean_text(parts[0], 32)
                mob_x, mob_y = int(parts[1]), int(parts[2])
                if len(parts) not in (3, 4) or (len(parts) == 4 and parts[3] not in ("0", "1")):
                    raise ValueError("mob boss flag")
                boss = len(parts) == 4 and parts[3] == "1"
            except (IndexError, ValueError):
                await send_json(player.writer, {"type": "error", "code": "mob_attack_invalid"})
                return False
            limit = 601 if boss else 301  # source getDistance truncates to int
            dx, dy = target.x - mob_x, target.y - mob_y
            if dx * dx + dy * dy >= limit * limit:
                await send_json(player.writer, {"type": "error", "code": "mob_attack_range"})
                return False
            if self.combat_effect(target, "invisible_ms"):
                await send_json(player.writer, {"type": "error", "code": "mob_target_invisible"})
                return False
            raw = max(0, min(int(message.get("damage", 0)), 2000000000))
            reduction = max(0, int(target.appearance.get("damage_down", 0)))
            system = max(0, min(int(message.get("skill", 0)), 3))
            resistance = int(target.appearance.get(
                "res_fire" if system == 1 else "res_ice" if system == 2 else "res_wind", 0))
            protected = target.revive_protected_until > time.monotonic() \
                or self.combat_effect(target, "miss_ms")
            shown = -1 if protected else max(1, raw - reduction - max(0, resistance)) if raw > 0 else 0
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
            amount = max(0, min(int(message.get("damage", 0)), MAX_YEN_TRANSFER))
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
            event = dict(base, type="world_packet", command=command, data=payload)
            if target is not None:
                await send_json(target.writer, event)
            else:
                await self.broadcast(room, event, player.player_id)
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
                          and member.map_id == player.map_id
                          and member.zone_id == player.zone_id]
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
            raw_ack = clean_text(message.get("data"), 340)
            delivery_id, separator, receipt = raw_ack.partition("|")
            event = room.pending_deliveries.get(player.player_id, {}).get(delivery_id)
            seller_event = None
            # Buyer commits debit+grant durably BEFORE ACK. Transfer the pending
            # obligation to the seller in one in-memory/persisted state change;
            # failed/full-bag buyers never release a seller payment.
            if (event is not None and event.get("type") == "shinwa_sold"
                    and event.get("buyer_id") == player.player_id):
                seller_id = str(event.get("seller_id", ""))
                if seller_id and seller_id != player.player_id:
                    seller_delivery = str(event.get("transaction", "")) + "-seller"
                    seller_event = dict(event, delivery_id=seller_delivery)
                    room.pending_deliveries.setdefault(seller_id, {})[seller_delivery] = seller_event
            # Activity points are committed on the first real pending ACK only,
            # alongside removing that obligation; failed/replayed ACK cannot add twice.
            changed_clan = None
            if event is not None and event.get("type") == "activity_reward":
                reward = str(event.get("data", "")).partition("|")[2]
                points = 0
                for row in reward.split(";"):
                    if row.startswith("p,"):
                        try: points += max(0, min(int(row[2:]), 2147483647))
                        except ValueError: pass
                changed_clan = self.clan_for_player(room, player.player_id) if points else None
                if changed_clan is not None:
                    member = changed_clan.get("members", {}).get(player.player_id)
                    if member is not None:
                        member["points"] = min(2147483647, int(member.get("points", 0)) + points)
                        changed_clan["exp"] = min(2147483647, int(changed_clan.get("exp", 0)) + points)
            removed = self.acknowledge_delivery(room, player.player_id, delivery_id)
            if not removed and not separator:
                await send_json(player.writer, {"type": "error", "code": "delivery_not_found"}); return False
            if separator and receipt:
                # Absent means no obligation remains in this room. Idempotent
                # confirmation lets a client retry a confirmation lost in transit.
                self.save_state(); await self.flush_state()
                await send_json(player.writer, {"type": "activity_state", "phase": "receipt_ack",
                                                "delivery_id": delivery_id, "data": receipt})
            if changed_clan is not None: await self.broadcast_clan(room, changed_clan)
            if seller_event is not None:
                await self.flush_state()
                seller = room.players.get(str(seller_event.get("seller_id", "")))
                if seller is not None:
                    try:
                        await send_json(seller.writer, seller_event)
                    except (ConnectionError, OSError):
                        pass  # obligation remains durable for seller reconnect
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
            if any(event.get("type") == "shinwa_sold"
                   and event.get("seller_id") == player.player_id
                   and str(event.get("data", "")).split(",", 1)[0] == str(slot)
                   for pending in room.pending_deliveries.values() for event in pending.values()):
                await send_json(player.writer, {"type": "error", "code": "shinwa_settling"})
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
            pending = next((event for event in room.pending_deliveries.get(player.player_id, {}).values()
                            if event.get("type") == "shinwa_sold"
                            and event.get("buyer_id") == player.player_id), None)
            if pending is not None:
                # The same product is a retry, not a new sale. A buyer cannot
                # accumulate mutually unaffordable/incompatible reservations.
                await send_json(player.writer, dict(pending))
                if int(pending.get("item_id", 0)) != product:
                    await send_json(player.writer, {"type": "error", "code": "shinwa_pending"})
                return True
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
            # Publish the obligation before any socket await; a disconnect
            # cannot pop the listing without leaving a replayable delivery.
            delivery_id = transaction + "-buyer"
            buyer_event = dict(sold, delivery_id=delivery_id)
            room.pending_deliveries.setdefault(player.player_id, {})[delivery_id] = buyer_event
            self.save_state()
            await self.flush_state()
            await self.broadcast_except(
                room, {"type": "shinwa_removed", "seq": room.sequence,
                       "item_id": product, "seller_id": seller_id},
                {player.player_id, seller_id})
            await send_json(player.writer, buyer_event)
            # Seller receives the sale only from delivery_ack after local apply.
        elif kind == "shinwa_reclaim":
            raw = clean_text(message.get("data"), 4096)
            fields = raw.split(",", 7)
            try:
                slot, template, quantity, expires = int(fields[0]), int(fields[1]), int(fields[2]), int(fields[6])
            except (ValueError, IndexError):
                await send_json(player.writer, {"type": "error", "code": "shinwa_invalid"}); return False
            if not 0 <= slot < 10 or template < 0 or quantity < 1 or expires > int(time.time() * 1000):
                await send_json(player.writer, {"type": "error", "code": "shinwa_invalid"}); return False
            pending = room.pending_deliveries.get(player.player_id, {})
            retry = next((event for event in pending.values() if event.get("type") == "shinwa_removed"
                          and event.get("category") == 1 and event.get("data") == raw), None)
            if retry is not None:
                await send_json(player.writer, retry); return True
            if any(event.get("type") == "shinwa_sold" and event.get("seller_id") == player.player_id
                   and str(event.get("data", "")).split(",", 1)[0] == str(slot)
                   for obligations in room.pending_deliveries.values() for event in obligations.values()):
                await send_json(player.writer, {"type": "error", "code": "shinwa_settling"}); return False
            product = next((key for key, row in room.shinwa.items() if row.get("seller_id") == player.player_id
                            and int(row.get("slot", -1)) == slot), 0)
            if product and room.shinwa[product].get("data") != raw:
                await send_json(player.writer, {"type": "error", "code": "shinwa_invalid"}); return False
            if product: room.shinwa.pop(product, None)
            delivery = "reclaim-" + secrets.token_hex(12)
            event = {"type": "shinwa_removed", "seq": room.sequence, "item_id": product,
                     "seller_id": player.player_id, "category": 1, "data": raw, "delivery_id": delivery}
            room.pending_deliveries.setdefault(player.player_id, {})[delivery] = event
            self.save_state(); await self.flush_state()
            try: await send_json(player.writer, event)
            except (ConnectionError, OSError): pass
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
            if category not in (0, 1) or amount < 0 or amount > MAX_YEN_TRANSFER \
                    or (winner_id and winner_id not in room.players):
                await send_json(player.writer, {"type": "error", "code": "vxmm_invalid_result"})
                return False
            room.lucky_bets[category] = {}
            await self.broadcast(room, {"type": "vxmm_result", "seq": room.sequence,
                                         "category": category, "target_id": winner_id,
                                         "name": clean_text(winner_name, MAX_NAME),
                                         "amount": amount, "data": str(winner_bet)})
            room.sequence += 1
            await self.broadcast(room, {"type": "chat", "seq": room.sequence,
                                         "channel": "world", "player_id": player.player_id,
                                         "actor_id": player.actor_id, "name": "Admin",
                                         "map": player.map_id, "zone": player.zone_id,
                                         "text": "Chúc mừng " + clean_text(winner_name, MAX_NAME).upper()
                                                 + " đã chiến thắng " + f"{amount:,}"
                                                 + " xu trong trò chơi Vòng xoay may mắn với "
                                                 + f"{winner_bet:,}" + " xu"})
        elif kind == "chan_le_bet":
            try:
                amount, side = [int(value) for value in str(message.get("data", "")).split(",", 1)]
            except (ValueError, TypeError):
                amount, side = 0, -1
            if amount < 1000000 or amount > 50000000 or side not in (0, 1):
                await send_json(player.writer, {"type": "error", "code": "chan_le_invalid_bet"})
                return False
            room.chan_le_bets[player.player_id] = {"amount": amount, "side": side}
            await self.broadcast(room, {"type": "chan_le_bet", "seq": room.sequence,
                                         "player_id": player.player_id, "name": player.name,
                                         "amount": amount, "relation_type": side})
        elif kind == "chan_le_result":
            if player.player_id != room.owner_id:
                await send_json(player.writer, {"type": "error", "code": "owner_world_only"})
                return False
            try:
                owner_round, side, first, second, result = [
                    int(value) for value in str(message.get("data", "")).split(",", 4)]
            except (ValueError, TypeError):
                owner_round, side, first, second, result = -1, -1, 0, 0, 0
            if owner_round < 0 or side not in (0, 1) or first < 0 or second < 0 or result < 0:
                await send_json(player.writer, {"type": "error", "code": "chan_le_invalid_result"})
                return False
            room.chan_le_bets = {}
            await self.broadcast(room, {"type": "chan_le_result", "seq": room.sequence,
                                         "category": owner_round, "relation_type": side,
                                         "amount": first, "quantity": second, "score": result})
        elif kind == "dungeon_finish":
            if player.player_id != room.owner_id:
                await send_json(player.writer, {"type": "error", "code": "owner_world_only"})
                return False
            parts = str(message.get("data", "")).split(",", 3)
            try: level, points, reward = int(parts[0]), int(parts[1]), int(parts[2])
            except (ValueError, IndexError):
                await send_json(player.writer, {"type": "error", "code": "dungeon_result_invalid"}); return False
            run_id = parts[3] if len(parts) > 3 else ""
            run = room.dungeon_runs.get(run_id)
            if run is None and not run_id:
                run = next((v for v in room.dungeon_runs.values() if v.get("owner") == player.player_id
                            and v.get("level") == level and not v.get("finished")), None)
            if (run is None or run.get("owner") != player.player_id or run.get("level") != level
                    or not 0 <= points <= 32767 or not 0 <= reward <= 32767):
                await send_json(player.writer, {"type": "error", "code": "dungeon_result_invalid"}); return False
            maps = ({91,92,93}, {94,95,96,97}, {105,106,107,108,109},
                    {114,115,116}, {125,126,127,128}, {157,158,159})
            deliveries = []
            if not run.get("finished"):
                run["finished"] = True
                for member_id in run["members"]:
                    member = room.players.get(member_id)
                    if member is None or member_id == player.player_id or member.map_id not in maps[level]: continue
                    delivery = "dungeon-" + run["id"] + "-" + member_id
                    event = {"type": "dungeon_finish", "seq": room.sequence, "category": level,
                             "amount": points, "quantity": reward, "owner_id": room.owner_id,
                             "data": run["id"], "delivery_id": delivery}
                    room.pending_deliveries.setdefault(member_id, {})[delivery] = event
                    deliveries.append((member, event))
                self.save_state(); await self.flush_state()
            for member, event in deliveries:
                try: await send_json(member.writer, event)
                except (ConnectionError, OSError): pass
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
