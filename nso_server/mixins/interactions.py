"""Interaction validation and domain dispatch."""

from __future__ import annotations

from typing import Any
import time

from ..config import clean_text
from ..models import Player, Room
from ..protocol import send_json


class InteractionsMixin:
    async def interaction(self, room: Room, player: Player,
                          message: dict[str, Any]) -> None:
        kind = clean_text(message.get("kind"), 32).lower()
        if kind in ("character_claim", "character_release"):
            identity = clean_text(message.get("text"), 32)
            if len(identity) != 16 or any(c not in "0123456789abcdef" for c in identity):
                await send_json(player.writer, {"type": "error", "code": "character_id_invalid"})
                return
            current = self.character_leases.get(identity)
            if kind == "character_release":
                if current is not None and current[0] is player:
                    self.character_leases.pop(identity, None)
                return
            now = time.monotonic()
            if current is not None and current[0] is not player:
                owner, until = current
                if self.players.get(owner.player_id) is owner or now < until:
                    await send_json(player.writer, {"type": "error", "code": "character_online", "data": identity})
                    return
            # No await between check and acquisition: the event-loop transaction
            # excludes other claimants across ALL rooms, not display-name aliases.
            for key, claim in list(self.character_leases.items()):
                if claim[0] is player and key != identity:
                    self.character_leases.pop(key, None)
            self.character_leases[identity] = (player, float("inf"))
            await send_json(player.writer, {"type": "character_claimed", "data": identity})
            return

        target = self.target_actor(room, message.get("target_actor"))
        if kind not in ("party_leave", "party_chat", "trade_cancel", "duel_cancel",
                        "party_lock", "dungeon_open",
                        "clan_chat", "clan_state_request", "clan_leave",
                        "clan_alert", "clan_contribute", "clan_points", "clan_upgrade",
                        "clan_item_upgrade", "clan_pet", "clan_territory_open",
                        "clan_store_buy", "clan_item_use", "clan_pet_sync",
                        "clan_war_accept", "clan_war_points",
                        "cuu_sat_clear", "shinwa_publish", "shinwa_buy",
                        "shinwa_remove", "vxmm_bet", "vxmm_result", "chan_le_bet",
                        "chan_le_result", "rank_report",
                        "dungeon_finish", "delivery_ack", "room_password",
                        "world_packet", "activity_reward", "party_buff") and target is None:
            await send_json(player.writer, {"type": "error", "code": "player_not_found"})
            return
        if target is player:
            await send_json(player.writer, {"type": "error", "code": "self_interaction"})
            return
        if target is not None and kind in ("trade_invite", "trade_accept",
                                           "duel_invite", "duel_accept", "duel_attack",
                                           "pvp_attack", "cuu_sat", "player_revive") \
                and (target.map_id != player.map_id
                     or target.zone_id != player.zone_id):
            await send_json(player.writer, {"type": "error", "code": "interaction_wrong_map"})
            return
        room.sequence += 1
        base = {"seq": room.sequence, "player_id": player.player_id,
                "actor_id": player.actor_id, "name": player.name,
                "map": player.map_id, "zone": player.zone_id}
        handled = False
        if kind in ('mob_attack', 'mob_yen', 'world_packet', 'activity_reward', 'room_password', 'room_kick', 'delivery_ack', 'friend_invite', 'shinwa_publish', 'shinwa_buy', 'shinwa_remove', 'vxmm_bet', 'vxmm_result', 'chan_le_bet', 'chan_le_result', 'dungeon_finish', 'rank_report', 'friend_remove'):
            handled = await self.handle_gameplay_interaction(
                room, player, target, message, base, kind)
        elif kind in ('clan_invite', 'clan_accept', 'clan_chat', 'clan_state_request', 'clan_alert', 'clan_role', 'clan_kick', 'clan_leave', 'clan_contribute', 'clan_points', 'clan_upgrade', 'clan_item_upgrade', 'clan_store_buy', 'clan_item_send', 'clan_item_use', 'clan_pet', 'clan_pet_sync', 'clan_territory_open', 'clan_war_invite', 'clan_war_accept', 'clan_war_points'):
            handled = await self.handle_clan_interaction(
                room, player, target, message, base, kind)
        elif kind in ('cuu_sat', 'cuu_sat_clear', 'pvp_attack', 'view_notice', 'party_buff', 'player_revive'):
            handled = await self.handle_combat_interaction(
                room, player, target, message, base, kind)
        elif kind in ('party_invite', 'party_accept', 'party_kick', 'party_leader', 'party_lock', 'dungeon_open', 'party_leave', 'party_chat'):
            handled = await self.handle_party_interaction(
                room, player, target, message, base, kind)
        elif kind in ('trade_invite', 'trade_accept', 'trade_offer', 'trade_confirm', 'trade_ready', 'trade_cancel', 'duel_invite', 'duel_accept', 'duel_attack', 'duel_cancel'):
            handled = await self.handle_trade_interaction(
                room, player, target, message, base, kind)
        else:
            await send_json(player.writer, {"type": "error", "code": "unknown_interaction"})
            return
        if not handled:
            return
        self.log("interaction", room=room.room_id, player_id=player.player_id,
                 target=getattr(target, "player_id", ""), kind=kind,
                 sequence=room.sequence)
        self.save_state()
