"""Combat validation and durable delivery helpers."""

from __future__ import annotations

import secrets
import time
from typing import Any

from ..models import Player, Room
from ..protocol import send_json

class CombatMixin:
    async def clear_cuu_sat_for_player(self, room: Room, player: Player,
                                       reason: str, advance_sequence: bool = False) -> bool:
        peer_id = room.cuu_sat.pop(player.player_id, "")
        if not peer_id:
            peer_id = next((left for left, right in room.cuu_sat.items()
                            if right == player.player_id), "")
            if peer_id:
                room.cuu_sat.pop(peer_id, None)
        peer = room.players.get(peer_id)
        if peer is None:
            return False
        if advance_sequence:
            room.sequence += 1
        await self.send_pair(player, peer, {
            "type": "cuu_sat_end", "seq": room.sequence,
            "first_id": player.player_id, "first_actor_id": player.actor_id,
            "second_id": peer.player_id, "second_actor_id": peer.actor_id,
            "reason": reason,
        })
        self.log("cuu_sat_ended", room=room.room_id,
                 first=player.player_id, second=peer.player_id, reason=reason,
                 sequence=room.sequence)
        return True

    async def durable_delivery(self, room: Room, player_id: str,
                               payload: dict[str, Any], prefix: str) -> str:
        delivery_id = prefix + "-" + secrets.token_hex(8)
        event = dict(payload, delivery_id=delivery_id)
        room.pending_deliveries.setdefault(player_id, {})[delivery_id] = event
        recipient = room.players.get(player_id)
        if recipient is not None:
            await send_json(recipient.writer, event)
        self.save_state()
        return delivery_id

    def acknowledge_delivery(self, room: Room, player_id: str,
                             delivery_id: str) -> bool:
        pending = room.pending_deliveries.get(player_id, {})
        removed = pending.pop(delivery_id, None) is not None
        if not pending:
            room.pending_deliveries.pop(player_id, None)
        if removed:
            self.save_state()
        return removed

    @staticmethod
    def authoritative_pvp_damage(attacker: Player, target: Player) -> int:
        attack = max(1, int(attacker.appearance.get("damage", 1)))
        reduction = max(0, int(target.appearance.get("damage_down", 0)))
        resistance = (max(0, int(target.appearance.get("res_fire", 0)))
                      + max(0, int(target.appearance.get("res_ice", 0)))
                      + max(0, int(target.appearance.get("res_wind", 0)))) // 3
        # NSO resistance is a flat combat stat. It may not reduce a valid hit
        # below one; client-reported damage is never accepted as authority.
        return max(1, attack - reduction - resistance)

    async def validate_pvp_attack(self, player: Player, target: Player) -> int:
        if (player.map_id != target.map_id
                or player.zone_id != target.zone_id):
            await send_json(player.writer, {"type": "error", "code": "interaction_wrong_map"})
            return 0
        if abs(player.x - target.x) > 600 or abs(player.y - target.y) > 400:
            await send_json(player.writer, {"type": "error", "code": "target_too_far"})
            return 0
        if player.hp <= 0 or target.hp <= 0:
            await send_json(player.writer, {"type": "error", "code": "combatant_dead"})
            return 0
        now = time.monotonic()
        if now - player.last_pvp_at < 0.30:
            await send_json(player.writer, {"type": "error", "code": "skill_cooldown"})
            return 0
        player.last_pvp_at = now
        return self.authoritative_pvp_damage(player, target)

