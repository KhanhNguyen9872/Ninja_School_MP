"""Protocol-v2 binary codec and wire dictionaries."""

from __future__ import annotations

import asyncio
import struct
from typing import Any

REQUEST_OPCODES = {1: "hello", 2: "create", 3: "join", 4: "resume",
                   5: "snapshot", 6: "state", 7: "map", 8: "attack",
                   9: "pickup", 10: "drop", 11: "chat", 12: "private_chat",
                   13: "interaction", 14: "appearance", 15: "clone",
                   16: "world_sync", 17: "ping", 18: "disconnect"}
EVENT_TYPES = ("welcome", "hello_ok", "room_joined", "error", "pong",
               "player_join", "player_leave", "snapshot", "player_appearance",
               "player_state", "map_transition", "mob_state", "drop_spawn",
               "drop_taken", "chat", "private_chat", "party_invite",
               "party_roster", "party_chat", "party_closed", "trade_invite",
               "trade_open", "trade_offer", "trade_confirmed", "trade_commit",
               "trade_cancel", "duel_invite", "duel_start", "duel_hit",
               "duel_end", "clone_state", "look_notice", "world_sync",
               "friend_invite", "friend_add", "friend_remove", "clan_invite",
               "clan_joined", "clan_chat", "cuu_sat_start", "cuu_sat_end",
               "pvp_hit", "name_assigned", "shinwa_listing", "shinwa_sold",
               "shinwa_removed", "vxmm_bet", "vxmm_result", "trade_prepare",
               "rank_state", "dungeon_finish", "party_reward", "dungeon_admit",
               "clan_state", "clan_territory", "clan_war_state",
               "room_password_changed", "world_packet", "party_kill",
               "mob_reward", "mob_hit", "clan_item_delivery")
EVENT_TYPES += ("activity_reward", "activity_state", "activity_closed",
                "clan_name_assigned", "party_buff", "player_revived",
                "chan_le_bet", "chan_le_result")
EVENT_OPCODES = {name: 64 + index for index, name in enumerate(EVENT_TYPES)}
WIRE_KEYS = ("cmd", "player_id", "protocol", "name", "room", "password",
             "map", "x", "y", "hp", "max_hp", "mob_id", "damage",
             "drop_template", "drop_id", "item_id", "template", "owner",
             "text", "target", "kind", "target_actor", "data", "skill",
             "class_id", "gender", "head", "weapon", "body", "leg", "level",
             "fashion", "mount_ids", "mount_upgrades", "mount_systems",
             "pet_template", "pet_boss", "bijuu_template", "bijuu_boss",
             "actor_id", "appearance", "players", "mobs", "drops", "seq",
             "alive", "first_id", "first_actor_id", "second_id",
             "second_actor_id", "attacker_id", "attacker_actor_id", "target_id",
             "target_actor_id", "loser_id", "loser_actor_id", "winner_id",
             "winner_actor_id", "peer_id", "peer_actor_id", "peer_name",
             "transaction", "peer_offer", "leader_id", "members", "time",
             "code", "max_mp", "mp", "speed", "res_fire", "res_ice",
             "res_wind", "damage_down", "exactly", "miss", "fatal",
             "react_damage", "sys_up", "sys_down", "equipment_ids",
             "equipment_upgrades", "equipment_systems", "fashion_ids",
             "fashion_upgrades", "fashion_systems", "clone", "active",
             "clone_actor_id", "clones", "owner_id", "payload", "world_epoch",
             "cheat_enabled", "clan_name", "relation_type", "joined_id",
             "joined_actor_id", "aggressor_id", "aggressor_actor_id", "reserved_names",
             "quantity", "locked", "upgrade", "expire", "expires_at", "picker_id",
             "shinwa", "seller_id", "seller_name", "buyer_id", "buyer_name", "category",
             "amount", "attacker", "resume_token", "delivery_id", "party_exp",
             "activity", "phase", "score", "zone", "respawn_seconds", "respawn_at",
             "protected_until", "item_type", "publisher")
KEY_TO_ID = {key: index + 1 for index, key in enumerate(WIRE_KEYS)}
ID_TO_KEY = {value: key for key, value in KEY_TO_ID.items()}

def _pack_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    if len(raw) > 65535:
        raw = raw[:65535]
    return struct.pack(">H", len(raw)) + raw

def _encode_value(value: Any) -> bytes:
    if value is None:
        return b"\x00"
    if value is False:
        return b"\x01"
    if value is True:
        return b"\x02"
    if isinstance(value, int):
        value = max(-(1 << 63), min(value, (1 << 63) - 1))
        encoded = (value << 1) ^ (value >> 63)
        result = bytearray((7,))
        while encoded & ~0x7f:
            result.append((encoded & 0x7f) | 0x80); encoded >>= 7
        result.append(encoded)
        return bytes(result)
    if isinstance(value, str):
        return b"\x04" + _pack_string(value)
    if isinstance(value, dict):
        rows = []
        for key, item in value.items():
            if key == "type":
                continue
            token = KEY_TO_ID.get(str(key), 0)
            rows.append(bytes((token,)) + (b"" if token else _pack_string(str(key)))
                        + _encode_value(item))
        return b"\x05" + struct.pack(">H", len(rows)) + b"".join(rows)
    if isinstance(value, (list, tuple)):
        return b"\x06" + struct.pack(">H", len(value)) + b"".join(_encode_value(v) for v in value)
    return b"\x04" + _pack_string(str(value))

def _decode_value(data: bytes, offset: int = 0) -> tuple[Any, int]:
    if offset >= len(data):
        raise ValueError("truncated value")
    tag = data[offset]; offset += 1
    if tag == 0: return None, offset
    if tag == 1: return False, offset
    if tag == 2: return True, offset
    if tag == 3:
        if offset + 8 > len(data): raise ValueError("truncated integer")
        return struct.unpack_from(">q", data, offset)[0], offset + 8
    if tag == 7:
        encoded = 0; shift = 0
        while shift < 70:
            if offset >= len(data): raise ValueError("truncated varint")
            value = data[offset]; offset += 1
            encoded |= (value & 0x7f) << shift
            if not value & 0x80:
                return (encoded >> 1) ^ -(encoded & 1), offset
            shift += 7
        raise ValueError("integer too long")
    if tag == 4:
        if offset + 2 > len(data): raise ValueError("truncated string")
        size = struct.unpack_from(">H", data, offset)[0]; offset += 2
        if offset + size > len(data): raise ValueError("truncated string bytes")
        return data[offset:offset + size].decode("utf-8"), offset + size
    if tag == 5:
        if offset + 2 > len(data): raise ValueError("truncated map")
        count = struct.unpack_from(">H", data, offset)[0]; offset += 2
        result: dict[str, Any] = {}
        for _ in range(count):
            if offset >= len(data): raise ValueError("truncated key")
            token = data[offset]; offset += 1
            if token:
                key = ID_TO_KEY.get(token)
                if key is None: raise ValueError("unknown key")
            else:
                key, offset = _decode_raw_string(data, offset)
            result[key], offset = _decode_value(data, offset)
        return result, offset
    if tag == 6:
        if offset + 2 > len(data): raise ValueError("truncated list")
        count = struct.unpack_from(">H", data, offset)[0]; offset += 2
        result = []
        for _ in range(count):
            item, offset = _decode_value(data, offset); result.append(item)
        return result, offset
    raise ValueError("unknown value tag")

def _decode_raw_string(data: bytes, offset: int) -> tuple[str, int]:
    if offset + 2 > len(data): raise ValueError("truncated key string")
    size = struct.unpack_from(">H", data, offset)[0]; offset += 2
    if offset + size > len(data): raise ValueError("truncated key bytes")
    return data[offset:offset + size].decode("utf-8"), offset + size

def encode_frame(payload: dict[str, Any]) -> bytes:
    opcode = EVENT_OPCODES.get(str(payload.get("type", "")))
    if opcode is None:
        raise ValueError(f"unknown outbound event {payload.get('type')!r}")
    body = _encode_value(payload)
    return b"NS" + bytes((2, opcode)) + struct.pack(">I", len(body)) + body

async def send_json(writer: Any, payload: dict[str, Any]) -> None:
    """Compatibility name; writes protocol-v2 binary frames, never JSON."""
    writer.write(encode_frame(payload))
    await writer.drain()
