"""In-memory room and player state."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class Player:
    player_id: str
    name: str
    writer: Any
    actor_id: int = 0
    room_id: str | None = None
    map_id: int = 1
    x: int = 0
    y: int = 0
    hp: int = 0
    appearance: dict[str, Any] = field(default_factory=dict)
    clone: dict[str, Any] = field(default_factory=dict)
    cheat_enabled: bool = False
    clan_name: str = ""
    resume_token_hash: str = ""
    last_pvp_at: float = 0.0
    last_seen: float = field(default_factory=time.monotonic)
    last_state: tuple[int, int, int, int] | None = None
    last_revive_at: float = 0.0

@dataclass
class Room:
    room_id: str
    owner_id: str = ""
    cheat_enabled: bool = False
    password_hash: str = ""
    players: dict[str, Player] = field(default_factory=dict)
    sequence: int = 0
    mobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    drops: dict[str, dict[str, Any]] = field(default_factory=dict)
    parties: dict[str, list[str]] = field(default_factory=dict)
    party_invites: dict[str, str] = field(default_factory=dict)
    party_locked: dict[str, bool] = field(default_factory=dict)
    party_dungeons: dict[str, int] = field(default_factory=dict)
    trades: dict[str, dict[str, Any]] = field(default_factory=dict)
    duels: dict[str, str] = field(default_factory=dict)
    friend_requests: dict[str, str] = field(default_factory=dict)
    friends: dict[str, list[str]] = field(default_factory=dict)
    clan_invites: dict[str, str] = field(default_factory=dict)
    cuu_sat: dict[str, str] = field(default_factory=dict)
    reserved_names: list[str] = field(default_factory=list)
    shinwa: dict[int, dict[str, Any]] = field(default_factory=dict)
    next_shinwa_id: int = 1000000
    lucky_bets: dict[int, dict[str, int]] = field(default_factory=lambda: {0: {}, 1: {}})
    rankings: dict[str, dict[str, int]] = field(default_factory=dict)
    pending_deliveries: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    clans: dict[str, dict[str, Any]] = field(default_factory=dict)

    def ensure_world(self) -> None:
        if not self.mobs:
            self.mobs["1:mob-1"] = {"mob_id": "mob-1", "template": 1, "map": 1,
                                   "x": 120, "y": 120, "hp": 100, "max_hp": 100,
                                   "alive": True}
