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
MAX_ROOM_DROPS = 128
SERVER_WRITE_HIGH_WATER = 256 * 1024
REPLACEABLE_EVENTS = frozenset(("player_state", "mob_state", "world_sync",
                                "clone_state"))
BUFFERED_RELIABLE_EVENTS = frozenset(("drop_spawn",))
HOT_PATH_COMMANDS = frozenset(("state", "world_sync", "attack", "drop", "ping"))
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

