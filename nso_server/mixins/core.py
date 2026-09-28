"""Connection handling, logging and broadcast primitives."""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..config import (BUFFERED_RELIABLE_EVENTS, HOT_PATH_COMMANDS, MAX_FRAME,
                      MAX_PASSWORD, MAX_ROOM, REPLACEABLE_EVENTS,
                      SERVER_WRITE_HIGH_WATER, clean_text, write_buffer_size)
from ..models import Player, Room
from ..protocol import REQUEST_OPCODES, _decode_value, encode_frame, send_json

class CoreMixin:
    def __init__(self, state_file: str | None = None,
                 trace_hot_path: bool = False) -> None:
        self.rooms: dict[str, Room] = {}
        self.players: dict[str, Player] = {}
        self.state_file = state_file
        self.saved_rooms: dict[str, list[dict[str, Any]]] = {}
        self.saved_worlds: dict[str, dict[str, Any]] = {}
        self._save_task: asyncio.Task[None] | None = None
        self._save_executor = ThreadPoolExecutor(max_workers=1,
                                                 thread_name_prefix="nso-state")
        self.trace_hot_path = trace_hot_path
        self._hot_commands: dict[str, int] = {}
        self._hot_log_at = time.monotonic()
        self.load_state()

    @staticmethod
    def log(event: str, **fields: Any) -> None:
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event}
        record.update(fields)
        # ASCII-escaped JSON keeps Windows cp1252 consoles from terminating a
        # live client handler when a name/chat contains Vietnamese or emoji.
        print(json.dumps(record, ensure_ascii=True, separators=(",", ":")), flush=True)

    def log_command(self, player: Player, command: str,
                    message: dict[str, Any]) -> None:
        fields = {"player_id": player.player_id, "name": player.name,
                  "room": player.room_id or clean_text(message.get("room"), MAX_ROOM).upper(),
                  "command": command, "map": message.get("map", player.map_id),
                  "password_supplied": bool(clean_text(message.get("password"), MAX_PASSWORD))}
        if self.trace_hot_path or command not in HOT_PATH_COMMANDS:
            self.log("command", **fields)
            return
        self.record_hot_path("command:" + command)

    def log_hot_path(self, event: str, **fields: Any) -> None:
        if self.trace_hot_path:
            self.log(event, **fields)
            return
        self.record_hot_path("event:" + event)

    def record_hot_path(self, key: str) -> None:
        self._hot_commands[key] = self._hot_commands.get(key, 0) + 1
        now = time.monotonic()
        if now - self._hot_log_at >= 1.0:
            self.log("hot_path_summary", commands=dict(self._hot_commands),
                     players=len(self.players), rooms=len(self.rooms))
            self._hot_commands.clear()
            self._hot_log_at = now

    async def broadcast(self, room: Room, payload: dict[str, Any], exclude: str | None = None) -> None:
        dead: list[str] = []; recipients = []; frame = encode_frame(payload)
        event_type = payload.get("type")
        # Living HP projections may be coalesced because a later hit repairs
        # them. A death transition may never be skipped: otherwise a guest
        # continues painting and targeting a mob which is dead in room state.
        reliable_mob_death = event_type == "mob_state" and not bool(
            payload.get("mob", {}).get("alive", True))
        reliable_mob_respawn = event_type == "mob_state" \
            and bool(payload.get("mob", {}).get("alive", False)) \
            and int(payload.get("attacker_actor_id", -1)) == 0
        # HP zero is a lifecycle edge, not a cosmetic position sample. Never
        # drop it under receiver backpressure or peers can keep a dead player
        # targetable and painted at the previous/full HP until another update.
        reliable_player_death = event_type == "player_state" and int(
            payload.get("hp", 1)) <= 0
        replaceable = event_type in REPLACEABLE_EVENTS \
            and not reliable_mob_death and not reliable_mob_respawn \
            and not reliable_player_death
        buffered_reliable = payload.get("type") in BUFFERED_RELIABLE_EVENTS
        for player_id, player in tuple(room.players.items()):
            if player_id == exclude:
                continue
            # Drop an obsolete projection for a slow receiver instead of
            # accumulating unbounded state; the following projection repairs
            # it.  Reliable events never take this path.
            if replaceable and write_buffer_size(player.writer) >= SERVER_WRITE_HIGH_WATER:
                continue
            try:
                player.writer.write(frame); recipients.append((player_id, player))
            except (ConnectionError, OSError):
                dead.append(player_id)
        should_drain = recipients and not replaceable and (not buffered_reliable or any(
            write_buffer_size(player.writer) >= SERVER_WRITE_HIGH_WATER
            for _, player in recipients))
        if should_drain:
            results = await asyncio.gather(*(p.writer.drain() for _, p in recipients),
                                           return_exceptions=True)
            dead.extend(player_id for (player_id, _), result in zip(recipients, results)
                        if isinstance(result, (ConnectionError, OSError)))
        for player_id in dead:
            await self.disconnect(room.players.get(player_id))

    async def broadcast_except(self, room: Room, payload: dict[str, Any],
                               excluded: set[str]) -> None:
        recipients = []; frame = encode_frame(payload)
        reliable_player_death = payload.get("type") == "player_state" and int(
            payload.get("hp", 1)) <= 0
        replaceable = payload.get("type") in REPLACEABLE_EVENTS \
            and not reliable_player_death
        buffered_reliable = payload.get("type") in BUFFERED_RELIABLE_EVENTS
        for player_id, candidate in tuple(room.players.items()):
            if player_id in excluded:
                continue
            if replaceable and write_buffer_size(candidate.writer) >= SERVER_WRITE_HIGH_WATER:
                continue
            try:
                candidate.writer.write(frame); recipients.append(candidate)
            except (ConnectionError, OSError):
                await self.disconnect(candidate)
        should_drain = recipients and not replaceable and (not buffered_reliable or any(
            write_buffer_size(candidate.writer) >= SERVER_WRITE_HIGH_WATER
            for candidate in recipients))
        if should_drain:
            results = await asyncio.gather(*(p.writer.drain() for p in recipients),
                                           return_exceptions=True)
            for candidate, result in zip(recipients, results):
                if isinstance(result, (ConnectionError, OSError)):
                    await self.disconnect(candidate)

    async def broadcast_map(self, room: Room, map_id: int,
                            payload: dict[str, Any]) -> None:
        """Encode once and project a reliable action only to visible peers."""
        frame = encode_frame(payload); recipients: list[Player] = []
        for candidate in tuple(room.players.values()):
            if candidate.map_id != map_id:
                continue
            try:
                candidate.writer.write(frame); recipients.append(candidate)
            except (ConnectionError, OSError):
                await self.disconnect(candidate)
        if recipients:
            results = await asyncio.gather(*(p.writer.drain() for p in recipients),
                                           return_exceptions=True)
            for candidate, result in zip(recipients, results):
                if isinstance(result, (ConnectionError, OSError)):
                    await self.disconnect(candidate)

    @staticmethod
    def target_actor(room: Room, actor_id: Any) -> Player | None:
        try:
            wanted = int(actor_id)
        except (TypeError, ValueError):
            return None
        return next((candidate for candidate in room.players.values()
                     if candidate.actor_id == wanted), None)

    async def send_pair(self, first: Player, second: Player,
                        payload: dict[str, Any]) -> None:
        for participant in (first, second):
            try:
                await send_json(participant.writer, payload)
            except (ConnectionError, OSError):
                pass

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw_socket = writer.get_extra_info("socket")
        if raw_socket is not None:
            try:
                raw_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except (AttributeError, OSError):
                pass
        player = Player(secrets.token_hex(6), "player", writer)
        player.actor_id = self.actor_id_for(player.player_id)
        self.players[player.player_id] = player
        peer = writer.get_extra_info("peername")
        self.log("connect", player_id=player.player_id, peer=str(peer), connections=len(self.players))
        try:
            await send_json(writer, {"type": "welcome", "player_id": player.player_id, "protocol": 2})
            while True:
                try:
                    header = await reader.readexactly(8)
                except asyncio.IncompleteReadError:
                    break
                if header[:2] != b"NS" or header[2] != 2:
                    await send_json(writer, {"type": "error", "code": "invalid_binary_header"})
                    break
                opcode = header[3]
                size = struct.unpack(">I", header[4:])[0]
                if size > MAX_FRAME:
                    await send_json(writer, {"type": "error", "code": "frame_too_large"})
                    break
                try:
                    raw = await reader.readexactly(size)
                    message, consumed = _decode_value(raw)
                except (asyncio.IncompleteReadError, UnicodeDecodeError, ValueError):
                    await send_json(writer, {"type": "error", "code": "invalid_binary_payload"})
                    continue
                if consumed != len(raw) or not isinstance(message, dict):
                    await send_json(writer, {"type": "error", "code": "object_required"})
                    continue
                command = REQUEST_OPCODES.get(opcode)
                if command is None:
                    await send_json(writer, {"type": "error", "code": "unknown_opcode"})
                    continue
                message["cmd"] = command
                player.last_seen = time.monotonic()
                await self.command(player, message)
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            await self.disconnect(player)
            if peer:
                self.log("disconnect", player_id=player.player_id, peer=str(peer),
                         room=player.room_id or "", connections=len(self.players))

