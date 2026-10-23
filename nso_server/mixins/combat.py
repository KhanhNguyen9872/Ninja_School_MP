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
    def combat_effect(player: Player, key: str) -> bool:
        combat = player.appearance.get("combat", {})
        elapsed = max(0.0, time.monotonic() * 1000 - int(combat.get("_at", int(time.monotonic() * 1000))))
        return int(combat.get(key, 0)) > elapsed

    @staticmethod
    def authoritative_pvp_damage(attacker: Player, target: Player) -> int:
        """Char.attackCharacter ordering, using bounded native appearance stats.

        A reported interaction damage is deliberately not an input. Profile
        updates remain private-RMS projections, like existing appearance stats.
        """
        a, b = attacker.appearance, target.appearance
        ac, bc = a.get("combat", {}), b.get("combat", {})
        def divide(value: int, divisor: int) -> int:
            # Java truncates toward zero, without a float precision loss for
            # large but bounded native option products.
            return -(abs(value) // divisor) if value < 0 else value // divisor
        def pct(value: int, percent: int) -> int:
            return divide(value * percent, 100)
        upper = max(1, int(a.get("damage", 1)))
        lower = max(0, min(upper, int(ac.get("damage2", upper))))
        base = lower + secrets.randbelow(upper - lower + 1)
        damage = base
        fatal = int(a.get("fatal", 0)) > secrets.randbelow(1000)
        stealth = CombatMixin.combat_effect(attacker, "stealth_ms")
        fatal = fatal or stealth
        if stealth:
            # One attack consumes source type11, never a fresh crit roll on
            # every attack until the next periodic appearance refresh.
            ac["stealth_ms"] = 0
            ac["invisible_ms"] = 0
        extra = 0
        if fatal:
            extra = base + pct(base, int(ac.get("fatal_percent", 0))
                              + int(ac.get("support_fatal", 0))
                              + (int(ac.get("stealth_fatal", 0)) if stealth else 0))
            extra += int(ac.get("fatal_damage", 0))
            extra -= pct(damage, int(bc.get("fatal_reduce", 0)))
        if CombatMixin.combat_effect(target, "burn_ms"):
            damage += base
        system = (int(a.get("class_id", 0)) + 1) // 2
        element = {1: ("fire", "55"), 2: ("ice", "56"), 3: ("wind", "54")}.get(system)
        if element:
            name, percent = element
            damage -= max(0, int(b.get("res_" + name, 0)))
            damage -= pct(damage, int(bc.get("res_" + name + "_pct", 0)))
            damage -= int(bc.get("res_" + name + "_flat", 0))
            damage += int(ac.get("atk_" + name, 0))
            damage += pct(base, int(ac.get("atk_pct" + percent, 0)))
            if system == 2 and fatal:
                extra += pct(damage, int(ac.get("support_fatal", 0)))
        extra -= pct(extra, min(100, int(bc.get("fatal_resist", 0))))
        damage -= pct(damage, int(bc.get("pvp_reduce", 0)))
        if CombatMixin.combat_effect(target, "reduction_ms"):
            damage -= pct(damage, int(bc.get("reduction_percent", 0)))
        level = int(b.get("level", 1)) - int(a.get("level", 1))
        damage = max(1, divide((damage + divide(extra, 3)) * (6 - divide(level, 40)), 100))
        miss = secrets.randbelow(max(1, int(b.get("miss", 0)) + 100)) \
            > secrets.randbelow(max(1, int(a.get("exactly", 0)) + 100))
        miss = miss or target.revive_protected_until > time.monotonic() \
            or CombatMixin.combat_effect(target, "miss_ms")
        damage += int(ac.get("flat113", 0)) + int(ac.get("flat103", 0))
        damage -= max(0, int(b.get("damage_down", 0)))
        attacker.last_pvp_fatal = fatal
        return 0 if miss else max(0, min(2000000000, damage))

    async def validate_pvp_attack(self, player: Player, target: Player,
                                  skill: int = -1, duel: bool = False) -> int:
        async def reject(code: str) -> int:
            await send_json(player.writer, {"type": "error", "code": code})
            return -1
        if player.map_id != target.map_id or player.zone_id != target.zone_id:
            return await reject("interaction_wrong_map")
        if player.hp <= 0 or target.hp <= 0:
            return await reject("combatant_dead")
        combat = player.appearance.get("combat", {})
        if combat.get("v") != 1 or int(combat.get("skill", -1)) != skill \
                or int(combat.get("attack_type", 0)) != 1:
            return await reject("combat_profile_missing")
        if skill == 24 and int(combat.get("bind_ms", 0)) <= 0:
            return await reject("combat_profile_missing")
        if int(player.appearance.get("weapon", -1)) < 0 \
                or self.combat_effect(player, "locked_ms"):
            return await reject("combatant_locked")
        if not duel and player.map_id in (1,10,17,22,27,32,38,43,48,72,98,133,162,177):
            return await reject("pvp_forbidden_map")
        if self.combat_effect(target, "invisible_ms"):
            return await reject("target_invisible")
        dx, dy = int(combat.get("dx", 0)), int(combat.get("dy", 0))
        lift = 40 if int(player.appearance.get("class_id", 0)) in (0,1,3,5) else 0
        if not (player.x-dx <= target.x <= player.x+dx
                and player.y-dy-lift <= target.y <= player.y+dy):
            return await reject("target_too_far")
        now = time.monotonic()
        if now < player.pvp_ready_at.get(skill, 0.0):
            return await reject("skill_cooldown")
        mana = int(combat.get("mana", 0))
        free = int(combat.get("free_mana", 0)) != 0
        if not free and int(player.appearance.get("mp", 0)) < mana:
            return await reject("not_enough_mp")
        player.last_pvp_at = now
        player.pvp_ready_at[skill] = now + int(combat.get("cooldown", 0)) / 1000.0
        if not free:
            player.appearance["mp"] = max(0, int(player.appearance.get("mp", 0)) - mana)
        if skill == 24:
            # NSO_FINAL Char.attackCharacter installs Effect18 and returns
            # before the damage/crit/stealth branch; protection is not a miss.
            target.pvp_bound_until = now + int(combat["bind_ms"]) / 1000.0
            player.last_pvp_fatal = False
            return 0
        return self.authoritative_pvp_damage(player, target)

    @staticmethod
    def pvp_bind_payload(target: Player, skill: int) -> dict[str, int]:
        if skill != 24:
            return {}
        remaining = max(0, int((target.pvp_bound_until - time.monotonic()) * 1000))
        return {"template": 18, "quantity": remaining}
