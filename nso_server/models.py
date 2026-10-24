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
    zone_id: int = 0
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
    last_state: tuple[int, int, int, int, int] | None = None
    last_revive_at: float = 0.0
    revive_protected_until: float = 0.0
    pvp_ready_at: dict[int, float] = field(default_factory=dict)
    last_pvp_fatal: bool = False
    # Transient authoritative Effect18/type14; never renewed by appearance.
    pvp_bound_until: float = 0.0
    hello_ok: bool = False
    peer_ip: str = ""
    connected_at: float = field(default_factory=time.monotonic)
    command_times: list[float] = field(default_factory=list, repr=False)

@dataclass
class Room:
    room_id: str
    owner_id: str = ""
    cheat_enabled: bool = False
    password_hash: str = ""
    players: dict[str, Player] = field(default_factory=dict)
    actor_index: dict[int, Player] = field(default_factory=dict, repr=False)
    # Room-lifetime primary/clone IDs stay reserved across guest reconnects.
    actor_ids: dict[str, int] = field(default_factory=dict, repr=False)
    sequence: int = 0
    mobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    drops: dict[str, dict[str, Any]] = field(default_factory=dict)
    parties: dict[str, list[str]] = field(default_factory=dict)
    party_invites: dict[str, str] = field(default_factory=dict)
    party_locked: dict[str, bool] = field(default_factory=dict)
    party_dungeons: dict[str, int] = field(default_factory=dict)
    dungeon_runs: dict[str, dict[str, Any]] = field(default_factory=dict)
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
    chan_le_bets: dict[str, dict[str, int]] = field(default_factory=dict)
    rankings: dict[str, dict[str, int]] = field(default_factory=dict)
    pending_deliveries: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    clan_contributions: dict[str, dict[str, Any]] = field(default_factory=dict)
    clans: dict[str, dict[str, Any]] = field(default_factory=dict)
    owner_grace_until: float = 0.0
    pending_invites: dict[str, float] = field(default_factory=dict)

    def ensure_world(self) -> None:
        """Rooms start with no server mob rows.

        Clients project their own map spawns; the old synthetic mob-1 row was
        replayed through room_joined/snapshot and revived a real map-1 mob slot.
        """
        pass
