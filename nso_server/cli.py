"""Command-line entry point and asyncio server bootstrap."""

from __future__ import annotations

import argparse
import asyncio

from .service import RoomServer
from .udp import start_udp_server


async def run(host: str, port: int, state_file: str | None,
              trace_hot_path: bool = False, transport_mode: str = "both") -> None:
    state = RoomServer(state_file, trace_hot_path=trace_hot_path)
    accepted = 0
    async def connection_task(reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter) -> None:
        nonlocal accepted
        accepted += 1
        task = asyncio.current_task()
        if task is not None:
            task.set_name(f"nso-client-{accepted}")
        await state.handle(reader, writer)

    # asyncio.start_server schedules one independent Task per accepted socket.
    # This gives concurrent I/O without one native thread (and stack) per J2ME
    # client; the shared room state remains serialized on the event loop while
    # disk snapshots continue on the dedicated executor.
    tcp_server = None
    udp_transport = None
    if transport_mode in ("tcp", "both"):
        tcp_server = await asyncio.start_server(connection_task, host, port, backlog=512)
    if transport_mode in ("udp", "both"):
        udp_transport, _ = await start_udp_server(state, host, port)
    listeners = []
    if tcp_server is not None:
        listeners.append("TCP " + ", ".join(
            str(sock.getsockname()) for sock in tcp_server.sockets or []))
    if udp_transport is not None:
        listeners.append("UDP " + str(udp_transport.get_extra_info("sockname")))
    print("nso-room-server listening on " + " | ".join(listeners), flush=True)
    try:
        if tcp_server is not None:
            async with tcp_server:
                await tcp_server.serve_forever()
        else:
            await asyncio.Future()
    finally:
        if udp_transport is not None:
            udp_transport.close()

def main() -> None:
    parser = argparse.ArgumentParser(description="NSO online room relay")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-file", default="online-server-state.json",
                        help="atomic room snapshot path; use an external volume in production")
    parser.add_argument("--trace-hot-path", action="store_true",
                        help="log every state/world/combat/drop frame (slower; default prints 1s summaries)")
    parser.add_argument("--transport", choices=("tcp", "udp", "both"), default="both",
                        help="listeners to start; clients default to TCP")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.host, args.port, args.state_file,
                        args.trace_hot_path, args.transport))
    except KeyboardInterrupt:
        pass
