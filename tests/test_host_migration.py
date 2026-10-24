import asyncio
import unittest
from unittest.mock import patch

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import receive, send


class HostMigrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from server import RoomServer
        self.state = RoomServer(quiet=True)
        self.server = await asyncio.start_server(self.state.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def connect(self, name):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        welcome = await receive(reader)
        await send(writer, {"cmd": "hello", "name": name})
        self.assertEqual((await receive(reader))["type"], "hello_ok")
        return reader, writer, welcome["player_id"]

    async def test_owner_disconnect_keeps_guests_then_resume(self):
        owner_reader, owner_writer, owner_id = await self.connect("Owner")
        await send(owner_writer, {"cmd": "create", "room": "GRACE"})
        owner_joined = await receive(owner_reader)
        token = owner_joined["resume_token"]
        guest_reader, guest_writer, _ = await self.connect("Guest")
        await send(guest_writer, {"cmd": "join", "room": "GRACE"})
        await receive(guest_reader)
        await receive(owner_reader)
        owner_writer.close()
        await owner_writer.wait_closed()
        waiting = await receive(guest_reader)
        self.assertEqual(waiting["type"], "owner_waiting")
        self.assertIn("GRACE", self.state.rooms)
        self.assertEqual(self.state.rooms["GRACE"].owner_id, owner_id)

        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        await receive(reader)
        await send(writer, {"cmd": "hello", "name": "Owner"})
        self.assertEqual((await receive(reader))["type"], "hello_ok")
        await send(writer, {"cmd": "resume", "player_id": owner_id,
                            "room": "GRACE", "resume_token": token})
        resumed = await receive(reader)
        self.assertEqual(resumed["type"], "room_joined")
        self.assertEqual(self.state.rooms["GRACE"].owner_id, owner_id)
        self.assertEqual(self.state.rooms["GRACE"].owner_grace_until, 0.0)
        writer.close()
        guest_writer.close()
        await writer.wait_closed()
        await guest_writer.wait_closed()

    async def test_promotion_purges_old_owner_state_and_announces_leave(self):
        owner_reader, owner_writer, owner_id = await self.connect("Owner")
        await send(owner_writer, {"cmd": "create", "room": "PURGE"})
        await receive(owner_reader)
        guest_reader, guest_writer, guest_id = await self.connect("Guest")
        await send(guest_writer, {"cmd": "join", "room": "PURGE"})
        await receive(guest_reader)
        await receive(owner_reader)
        room = self.state.rooms["PURGE"]
        room.parties[owner_id] = [owner_id, guest_id]
        room.duels[owner_id] = guest_id
        room.duels[guest_id] = owner_id
        owner_writer.close()
        await owner_writer.wait_closed()
        self.assertEqual((await receive(guest_reader))["type"], "owner_waiting")
        room.owner_grace_until = 0.0001
        await self.state.promote_or_close_room(room)
        seen = []
        for _ in range(8):
            seen.append(await receive(guest_reader))
            if any(m["type"] == "owner_changed" for m in seen):
                break
        types = [m["type"] for m in seen]
        self.assertIn("player_leave", types)
        self.assertIn("owner_changed", types)
        self.assertEqual(room.owner_id, guest_id)
        self.assertNotIn(owner_id, room.parties)
        self.assertNotIn(owner_id, room.duels)
        self.assertNotIn(guest_id, room.duels)
        guest_writer.close()
        await guest_writer.wait_closed()

    async def test_owner_leave_closes_room(self):
        owner_reader, owner_writer, _ = await self.connect("Owner")
        await send(owner_writer, {"cmd": "create", "room": "LEAVE"})
        await receive(owner_reader)
        guest_reader, guest_writer, _ = await self.connect("Guest")
        await send(guest_writer, {"cmd": "join", "room": "LEAVE"})
        await receive(guest_reader)
        await receive(owner_reader)
        await send(owner_writer, {"cmd": "leave"})
        closed = await receive(guest_reader)
        self.assertEqual((closed["type"], closed.get("code")),
                         ("error", "room_owner_left"))
        for _ in range(30):
            if "LEAVE" not in self.state.rooms:
                break
            await asyncio.sleep(0.01)
        self.assertNotIn("LEAVE", self.state.rooms)
        guest_writer.close()
        await guest_writer.wait_closed()

    async def test_grace_timeout_promotes_oldest_guest(self):
        with patch("nso_server.mixins.lifecycle.OWNER_GRACE_SECONDS", 0.05):
            owner_reader, owner_writer, owner_id = await self.connect("Owner")
            await send(owner_writer, {"cmd": "create", "room": "PROMOTE"})
            await receive(owner_reader)
            first_reader, first_writer, first_id = await self.connect("First")
            await send(first_writer, {"cmd": "join", "room": "PROMOTE"})
            await receive(first_reader)
            await receive(owner_reader)
            second_reader, second_writer, _ = await self.connect("Second")
            await send(second_writer, {"cmd": "join", "room": "PROMOTE"})
            await receive(second_reader)
            await receive(owner_reader)
            await receive(first_reader)
            owner_writer.close()
            await owner_writer.wait_closed()
            self.assertEqual((await receive(first_reader))["type"], "owner_waiting")
            self.assertEqual((await receive(second_reader))["type"], "owner_waiting")
            changed = None
            for _ in range(40):
                try:
                    changed = await asyncio.wait_for(receive(first_reader), 0.05)
                    if changed.get("type") == "owner_changed":
                        break
                except asyncio.TimeoutError:
                    pass
            self.assertIsNotNone(changed)
            self.assertEqual(changed["type"], "owner_changed")
            self.assertEqual(changed["owner_id"], first_id)
            self.assertEqual(self.state.rooms["PROMOTE"].owner_id, first_id)
            first_writer.close()
            second_writer.close()
            await first_writer.wait_closed()
            await second_writer.wait_closed()

    async def test_empty_grace_closes_room(self):
        owner_reader, owner_writer, _ = await self.connect("Solo")
        await send(owner_writer, {"cmd": "create", "room": "EMPTY"})
        await receive(owner_reader)
        owner_writer.close()
        await owner_writer.wait_closed()
        for _ in range(30):
            if "EMPTY" not in self.state.rooms:
                break
            await asyncio.sleep(0.01)
        self.assertNotIn("EMPTY", self.state.rooms)
