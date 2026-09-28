from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from ..config import MAX_NAME, MAX_PASSWORD, clean_text, password_digest
from ..models import Player, Room
from ..protocol import send_json

class TradeInteractionMixin:
    async def handle_trade_interaction(
            self, room: Room, player: Player, target: Player | None,
            message: dict[str, Any], base: dict[str, Any], kind: str) -> bool:
        if kind == "trade_invite":
            await send_json(target.writer, dict(base, type="trade_invite"))
        elif kind == "trade_accept":
            trade_id = "-".join(sorted((player.player_id, target.player_id)))
            room.trades[trade_id] = {"players": [target.player_id, player.player_id],
                                     "offers": {}, "accepts": []}
            await send_json(player.writer, {"type": "trade_open", "seq": room.sequence,
                                            "peer_id": target.player_id,
                                            "peer_actor_id": target.actor_id, "peer_name": target.name})
            await send_json(target.writer, {"type": "trade_open", "seq": room.sequence,
                                            "peer_id": player.player_id,
                                            "peer_actor_id": player.actor_id, "peer_name": player.name})
        elif kind in ("trade_offer", "trade_confirm", "trade_ready", "trade_cancel"):
            trade_key = next((key for key, value in room.trades.items()
                              if player.player_id in value.get("players", [])), "")
            trade = room.trades.get(trade_key)
            if trade is None:
                await send_json(player.writer, {"type": "error", "code": "trade_not_open"})
                return False
            peer_id = next((value for value in trade["players"] if value != player.player_id), "")
            peer = room.players.get(peer_id)
            if kind == "trade_offer":
                offer = clean_text(message.get("data"), 12000)
                trade["offers"][player.player_id] = offer
                trade["accepts"] = []
                if peer is not None:
                    await send_json(peer.writer, dict(base, type="trade_offer", data=offer))
            elif kind == "trade_confirm":
                if player.player_id not in trade["accepts"]:
                    trade["accepts"].append(player.player_id)
                if peer is not None:
                    await send_json(peer.writer, dict(base, type="trade_confirmed"))
                if len(trade["accepts"]) == 2 and len(trade["offers"]) == 2 and peer is not None:
                    transaction = secrets.token_hex(8)
                    trade["transaction"] = transaction; trade["ready"] = []
                    for participant_id in trade["players"]:
                        participant = room.players.get(participant_id)
                        other_id = next(value for value in trade["players"] if value != participant_id)
                        if participant is not None:
                            await send_json(participant.writer,
                                            {"type": "trade_prepare", "seq": room.sequence,
                                             "transaction": transaction,
                                             "peer_offer": trade["offers"].get(other_id, "")})
            elif kind == "trade_ready":
                transaction = clean_text(message.get("data"), 64)
                if not transaction or transaction != trade.get("transaction"):
                    await send_json(player.writer, {"type": "error", "code": "trade_transaction_mismatch"})
                    return False
                ready = trade.setdefault("ready", [])
                if player.player_id not in ready: ready.append(player.player_id)
                if len(ready) == 2 and peer is not None:
                    for participant_id in trade["players"]:
                        participant = room.players.get(participant_id)
                        other_id = next(value for value in trade["players"] if value != participant_id)
                        if participant is not None:
                            await self.durable_delivery(
                                room, participant_id,
                                {"type": "trade_commit", "seq": room.sequence,
                                 "transaction": transaction,
                                 "peer_offer": trade["offers"].get(other_id, "")},
                                "trade-" + transaction + "-" + participant_id)
                    room.trades.pop(trade_key, None)
            else:
                payload = {"type": "trade_cancel", "seq": room.sequence}
                if peer is not None:
                    await send_json(peer.writer, payload)
                await send_json(player.writer, payload)
                room.trades.pop(trade_key, None)
        elif kind == "duel_invite":
            await send_json(target.writer, dict(base, type="duel_invite"))
        elif kind == "duel_accept":
            room.duels[player.player_id] = target.player_id
            room.duels[target.player_id] = player.player_id
            await self.send_pair(player, target,
                                 {"type": "duel_start", "seq": room.sequence,
                                  "first_id": target.player_id, "first_actor_id": target.actor_id,
                                  "second_id": player.player_id, "second_actor_id": player.actor_id})
        elif kind == "duel_attack":
            if room.duels.get(player.player_id) != target.player_id:
                await send_json(player.writer, {"type": "error", "code": "duel_not_open"})
                return False
            damage = await self.validate_pvp_attack(player, target)
            if damage <= 0:
                return False
            skill = max(0, min(int(message.get("skill", 0)), 127))
            target.hp = max(0, target.hp - damage)
            await self.broadcast_map(room, player.map_id,
                                     {"type": "duel_hit", "seq": room.sequence,
                                      "attacker_id": player.player_id,
                                      "attacker_actor_id": player.actor_id,
                                      "target_id": target.player_id,
                                      "target_actor_id": target.actor_id,
                                      "skill": skill, "damage": damage,
                                      "hp": target.hp}, player.zone_id)
            if target.hp == 0:
                await self.send_pair(player, target,
                                     {"type": "duel_end", "seq": room.sequence,
                                      "loser_id": target.player_id,
                                      "loser_actor_id": target.actor_id,
                                      "winner_id": player.player_id,
                                      "winner_actor_id": player.actor_id})
                room.duels.pop(player.player_id, None)
                room.duels.pop(target.player_id, None)
                for participant in (player, target):
                    participant.hp = max(1, int(participant.appearance.get("max_hp", 1)))
        elif kind == "duel_cancel":
            peer_id = room.duels.pop(player.player_id, "")
            peer = room.players.get(peer_id)
            if peer is not None:
                room.duels.pop(peer_id, None)
                await self.send_pair(player, peer,
                                     {"type": "duel_end", "seq": room.sequence,
                                      "loser_id": player.player_id,
                                      "loser_actor_id": player.actor_id,
                                      "winner_id": peer.player_id,
                                      "winner_actor_id": peer.actor_id})
        return True
