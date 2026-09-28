"""Connection-like asyncio adapter for protocol-v2 UDP datagrams."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from .config import MAX_DATAGRAM, MAX_FRAME


UDP_IDLE_TIMEOUT = 75.0


@dataclass
class _UdpPeer:
    reader: asyncio.StreamReader
    writer: "UdpWriter"
    task: asyncio.Task[Any]
    last_seen: float


class UdpWriter:
    """Small StreamWriter-compatible facade used by the shared RoomServer."""

    def __init__(self, owner: "UdpServerProtocol", address: tuple[Any, ...]):
        self.owner = owner
        self.address = address
        self.transport: asyncio.DatagramTransport | None = None
        self._closing = False

    def attach(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def write(self, data: bytes) -> None:
        if self._closing or self.transport is None:
            raise ConnectionError("UDP peer is closed")
        if len(data) > MAX_DATAGRAM:
            raise OSError("UDP frame exceeds datagram limit")
        self.transport.sendto(data, self.address)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        if not self._closing:
            self._closing = True
            self.owner.drop_peer(self.address)

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)

    def is_closing(self) -> bool:
        return self._closing

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "peername":
            return self.address
        if name == "transport":
            return "udp"
        return default


class UdpServerProtocol(asyncio.DatagramProtocol):
    """Feeds validated one-frame datagrams into the normal stream handler."""

    def __init__(self, state: Any):
        self.state = state
        self.transport: asyncio.DatagramTransport | None = None
        self.peers: dict[tuple[Any, ...], _UdpPeer] = {}
        self._reaper: asyncio.Task[Any] | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self._reaper = asyncio.create_task(self._reap_idle(), name="nso-udp-reaper")

    def datagram_received(self, data: bytes, address: tuple[Any, ...]) -> None:
        if len(data) < 8 or len(data) > MAX_DATAGRAM or data[:3] != b"NS\x02":
            return
        size = int.from_bytes(data[4:8], "big")
        if size > MAX_FRAME or size != len(data) - 8:
            return
        peer = self.peers.get(address)
        if peer is None:
            if data[3] == 18:
                return
            if self.transport is None:
                return
            reader = asyncio.StreamReader(limit=MAX_FRAME + 8)
            writer = UdpWriter(self, address)
            writer.attach(self.transport)
            task = asyncio.create_task(
                self.state.handle(reader, writer),
                name=f"nso-udp-{address[0]}-{address[1]}",
            )
            peer = _UdpPeer(reader, writer, task, time.monotonic())
            self.peers[address] = peer
        peer.last_seen = time.monotonic()
        peer.reader.feed_data(data)

    def drop_peer(self, address: tuple[Any, ...]) -> None:
        peer = self.peers.pop(address, None)
        if peer is not None:
            peer.writer._closing = True
            peer.reader.feed_eof()

    async def _reap_idle(self) -> None:
        try:
            while True:
                await asyncio.sleep(5.0)
                expired = [address for address, peer in self.peers.items()
                           if time.monotonic() - peer.last_seen > UDP_IDLE_TIMEOUT]
                for address in expired:
                    self.drop_peer(address)
        except asyncio.CancelledError:
            pass

    def connection_lost(self, exc: Exception | None) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
        for address in tuple(self.peers):
            self.drop_peer(address)


async def start_udp_server(state: Any, host: str, port: int) -> tuple[
        asyncio.DatagramTransport, UdpServerProtocol]:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: UdpServerProtocol(state), local_addr=(host, port))
    return transport, protocol  # type: ignore[return-value]
