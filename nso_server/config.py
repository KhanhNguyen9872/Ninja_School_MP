"""Runtime limits and small validation helpers."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

MAX_FRAME = 256 * 1024
MAX_DATAGRAM = 65507
MAX_NAME = 24
MAX_ROOM = 32
MAX_PASSWORD = 64
MAX_ROOM_PLAYERS = 12
MAX_ROOMS = 64
MAX_ROOM_DROPS = 128
MAX_CONNECTIONS_PER_IP = 16
COMMANDS_PER_SEC = 15
COMMANDS_PER_SEC_HOT = 40
TCP_IDLE_SECONDS = 90.0
HANDSHAKE_SECONDS = 10.0
PASSWORD_FAIL_LIMIT = 5
PASSWORD_LOCK_SECONDS = 60.0
OWNER_GRACE_SECONDS = 45.0
INVITE_TTL_SECONDS = 30.0
MAX_YEN_TRANSFER = 50_000_000
MAX_DAMAGE = 1_000_000
SERVER_WRITE_HIGH_WATER = 256 * 1024
REPLACEABLE_EVENTS = frozenset(("player_state", "mob_state", "world_sync",
                                "clone_state"))
BUFFERED_RELIABLE_EVENTS = frozenset(("drop_spawn",))
HOT_PATH_COMMANDS = frozenset(("state", "world_sync", "attack", "drop", "ping",
                               "snapshot", "chat", "private_chat", "interaction"))
# Console keeps only lifecycle/failure events unless --verbose or --logs is used.
IMPORTANT_LOG_EVENTS = frozenset((
    "room_created", "room_joined", "room_left", "room_closed_owner_left",
    "owner_waiting", "owner_changed",
    "join_rejected", "resume_rejected", "room_player_kicked",
    "room_password_changed", "stale_rooms_discarded", "character_claimed",
    "server_start", "server_stop", "warning", "error",
))
OWNER_RESPONSE_TIMEOUT = 6.0

def clean_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]

def password_digest(password: str) -> str:
    if not password:
        return ""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def write_buffer_size(writer: Any) -> int:
    """Return queued TCP bytes without relying on private asyncio internals."""
    transport = getattr(writer, "transport", None)
    getter = getattr(transport, "get_write_buffer_size", None)
    if getter is None:
        return 0
    try:
        return int(getter())
    except (AttributeError, OSError, TypeError, ValueError):
        return 0

