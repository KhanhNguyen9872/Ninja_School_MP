"""Command-line entry point and asyncio server bootstrap."""

from __future__ import annotations

import argparse
import asyncio

from .service import RoomServer


async def run(host: str, port: int, state_file: str | None,
              trace_hot_path: bool = False) -> None:
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
    server = await asyncio.start_server(connection_task, host, port, backlog=512)
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"nso-room-server listening on {sockets}", flush=True)
    async with server:
        await server.serve_forever()

def main() -> None:
    parser = argparse.ArgumentParser(description="NSO online room relay")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-file", default="online-server-state.json",
                        help="atomic room snapshot path; use an external volume in production")
    parser.add_argument("--trace-hot-path", action="store_true",
                        help="log every state/world/combat/drop frame (slower; default prints 1s summaries)")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.host, args.port, args.state_file, args.trace_hot_path))
    except KeyboardInterrupt:
        pass
