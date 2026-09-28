from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from ..config import MAX_NAME, MAX_PASSWORD, clean_text, password_digest
from ..models import Player, Room
from ..protocol import send_json

class CombatInteractionMixin:
    async def handle_combat_interaction(
            self, room: Room, player: Player, target: Player | None,
            message: dict[str, Any], base: dict[str, Any], kind: str) -> bool:
        if kind == "cuu_sat":
            room.cuu_sat[player.player_id] = target.player_id
            await self.send_pair(player, target, {"type": "cuu_sat_start", "seq": room.sequence,
                                                   "aggressor_id": player.player_id,
                                                   "aggressor_actor_id": player.actor_id,
                                                   "target_id": target.player_id,
                                                   "target_actor_id": target.actor_id})
        elif kind == "cuu_sat_clear":
            await self.clear_cuu_sat_for_player(room, player, "manual_clear")
        elif kind == "pvp_attack":
            linked = room.cuu_sat.get(player.player_id) == target.player_id \
                or room.cuu_sat.get(target.player_id) == player.player_id
            if not linked:
                await send_json(player.writer, {"type": "error", "code": "cuu_sat_not_open"})
                return False
            damage = await self.validate_pvp_attack(player, target)
            if damage <= 0:
                return False
            skill = max(0, min(int(message.get("skill", 0)), 127))
            target.hp = max(0, target.hp - damage)
            await self.broadcast_map(room, player.map_id,
                                     {"type": "pvp_hit", "seq": room.sequence,
                                      "attacker_id": player.player_id,
                                      "attacker_actor_id": player.actor_id,
                                      "target_id": target.player_id,
                                      "target_actor_id": target.actor_id,
                                      "skill": skill, "damage": damage,
                                      "hp": target.hp}, player.zone_id)
            if target.hp == 0:
                room.cuu_sat.pop(player.player_id, None)
                room.cuu_sat.pop(target.player_id, None)
                await self.send_pair(player, target,
                                     {"type": "cuu_sat_end", "seq": room.sequence,
                                      "first_id": player.player_id,
                                      "first_actor_id": player.actor_id,
                                      "second_id": target.player_id,
                                      "second_actor_id": target.actor_id})
        elif kind == "view_notice":
            await send_json(target.writer, dict(base, type="look_notice"))
        elif kind == "party_buff":
            _, members = self.party_for(room, player.player_id)
            if not members:
                await send_json(player.writer, {"type": "error", "code": "party_required"})
                return False
            skill = max(0, min(int(message.get("skill", 0)), 127))
            expected_effect = {47: 8, 51: 19, 52: 20}.get(skill)
            try:
                values = [int(value) for value in str(message.get("data", "")).split(",")]
            except ValueError:
                values = []
            if expected_effect is None or len(values) != 6 or values[0] != expected_effect:
                await send_json(player.writer, {"type": "error", "code": "party_buff_invalid"})
                return False
            effect, duration, param, param2, dx, dy = values
            duration = max(1, min(duration, 600000))
            param = max(0, min(param, 32767)); param2 = max(0, min(param2, 32767))
            dx = max(0, min(dx, 2000)); dy = max(0, min(dy, 2000))
            range_squared = dx * dx + dy * dy
            for member_id in tuple(members):
                recipient = room.players.get(member_id)
                if recipient is None or recipient.map_id != player.map_id \
                        or recipient.zone_id != player.zone_id \
                        or recipient.hp <= 0:
                    continue
                delta_x = recipient.x - player.x; delta_y = recipient.y - player.y
                if recipient is not player \
                        and delta_x * delta_x + delta_y * delta_y > range_squared:
                    continue
                await self.broadcast_map(room, player.map_id,
                                         {"type": "party_buff", "seq": room.sequence,
                                          "attacker_id": player.player_id,
                                          "attacker_actor_id": player.actor_id,
                                          "target_id": recipient.player_id,
                                          "target_actor_id": recipient.actor_id,
                                          "skill": skill, "template": effect,
                                          "amount": duration, "damage": param,
                                          "quantity": param2}, player.zone_id)
        elif kind == "player_revive":
            # NSO_FINAL Char.hoiSinh: an alive class-6 character may cast
            # template 49 on one exhausted same-map character. Lôi đài and
            # Đấu trường explicitly reject both assisted and self revival.
            skill = max(0, min(int(message.get("skill", 0)), 127))
            try:
                revive_range, evasion = [int(value) for value in
                                          str(message.get("data", "")).split(",")]
            except (TypeError, ValueError):
                revive_range, evasion = 0, 0
            forbidden = player.map_id in (110, 111, 129, 149, 160, 161)
            if forbidden:
                await send_json(player.writer,
                                {"type": "error", "code": "revive_forbidden_map"})
                return False
            if int(player.appearance.get("class_id", 0)) != 6 or player.hp <= 0 \
                    or skill != 49:
                await send_json(player.writer,
                                {"type": "error", "code": "revive_caster_invalid"})
                return False
            if target.hp > 0:
                await send_json(player.writer,
                                {"type": "error", "code": "revive_target_alive"})
                return False
            revive_range = max(0, min(revive_range, 2000))
            evasion = max(0, min(evasion, 32767))
            dx = target.x - player.x; dy = target.y - player.y
            if revive_range <= 0 or dx * dx + dy * dy > revive_range * revive_range:
                await send_json(player.writer,
                                {"type": "error", "code": "revive_target_too_far"})
                return False
            now = time.monotonic()
            if now - player.last_revive_at < 0.30:
                await send_json(player.writer,
                                {"type": "error", "code": "skill_cooldown"})
                return False
            player.last_revive_at = now
            target.hp = max(1, int(target.appearance.get("max_hp", 1)))
            target.last_state = None
            await self.broadcast_map(room, player.map_id,
                                     {"type": "player_revived", "seq": room.sequence,
                                      "attacker_id": player.player_id,
                                      "attacker_actor_id": player.actor_id,
                                      "target_id": target.player_id,
                                      "target_actor_id": target.actor_id,
                                      "skill": 49, "hp": target.hp,
                                      "mp": max(1, int(target.appearance.get("max_mp", 1))),
                                      "x": target.x, "y": target.y,
                                      "template": 11, "amount": 5000,
                                      "damage": evasion}, player.zone_id)
        return True
