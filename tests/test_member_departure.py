import asyncio
import struct
import unittest

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import receive, send

from nso_server.protocol import MAX_DECODE_DEPTH, _decode_value


async def expect(reader, kind):
    """Skip unrelated projections until the wanted event arrives."""
    for _ in range(12):
        event = await receive(reader)
        if event["type"] == kind:
            return event
    raise AssertionError("missing " + kind)


class MemberDepartureTest(unittest.IsolatedAsyncioTestCase):
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

    async def room(self, name):
        owner = await self.connect("Owner")
        await send(owner[1], {"cmd": "create", "room": name})
        await receive(owner[0])
        guest = await self.connect("Guest")
        await send(guest[1], {"cmd": "join", "room": name})
        await receive(guest[0])
        await receive(owner[0])
        return owner, guest, self.state.rooms[name]

    async def test_guest_socket_loss_ends_cuu_sat_and_drops_invites(self):
        (owner_reader, owner_writer, owner_id), \
            (_, guest_writer, guest_id), room = await self.room("DEPART1")
        room.cuu_sat[guest_id] = owner_id
        room.party_invites[owner_id] = guest_id
        room.clan_invites[guest_id] = owner_id
        room.friend_requests[owner_id] = guest_id
        room.pending_invites[f"trade:{guest_id}:{owner_id}"] = 1e18
        room.pending_invites[f"duel:{owner_id}:{guest_id}"] = 1e18
        room.pending_invites[f"duel:{owner_id}:someone"] = 1e18
        guest_writer.close()
        await guest_writer.wait_closed()
        ended = await expect(owner_reader, "cuu_sat_end")
        self.assertEqual((ended["first_id"], ended["second_id"]), (guest_id, owner_id))
        await expect(owner_reader, "player_leave")
        self.assertEqual(room.cuu_sat, {})
        self.assertEqual(room.party_invites, {})
        self.assertEqual(room.clan_invites, {})
        self.assertEqual(room.friend_requests, {})
        self.assertEqual(list(room.pending_invites), [f"duel:{owner_id}:someone"])
        owner_writer.close()
        await owner_writer.wait_closed()

    async def test_voluntary_leave_releases_party_trade_and_duel(self):
        (owner_reader, owner_writer, owner_id), \
            (_, guest_writer, guest_id), room = await self.room("DEPART2")
        room.parties[owner_id] = [owner_id, guest_id]
        room.trades["t"] = {"players": [owner_id, guest_id]}
        room.duels[owner_id] = guest_id
        room.duels[guest_id] = owner_id
        await send(guest_writer, {"cmd": "leave"})
        seen = set()
        while "player_leave" not in seen:
            seen.add((await receive(owner_reader))["type"])
        self.assertTrue({"party_closed", "trade_cancel", "duel_end"} <= seen, seen)
        self.assertEqual((room.parties, room.trades, room.duels), ({}, {}, {}))
        self.assertIn("DEPART2", self.state.rooms)
        guest_writer.close(); owner_writer.close()
        await guest_writer.wait_closed(); await owner_writer.wait_closed()

    async def test_empty_room_survives_owner_grace_and_forgets_rows_after(self):
        (_, owner_writer, owner_id), (guest_reader, guest_writer, _), room = \
            await self.room("DEPART3")
        owner_writer.close()
        await owner_writer.wait_closed()
        await expect(guest_reader, "owner_waiting")
        guest_writer.close()
        await guest_writer.wait_closed()
        for _ in range(50):
            if not room.players:
                break
            await asyncio.sleep(0.01)
        # The owner may still resume, so the empty room and its rows stay.
        self.assertIs(self.state.rooms.get("DEPART3"), room)
        self.assertIsNotNone(self.state.saved_player_row("DEPART3", owner_id))
        room.owner_grace_until = 0.0
        await self.state.promote_or_close_room(room)
        self.assertNotIn("DEPART3", self.state.rooms)
        self.assertNotIn("DEPART3", self.state.saved_rooms)

    async def test_unexpected_command_error_keeps_the_connection(self):
        (owner_reader, owner_writer, _), (_, guest_writer, _), _ = \
            await self.room("DEPART4")

        async def broken(room, player, message):
            raise IndexError("simulated handler defect")

        self.state.interaction = broken
        await send(owner_writer, {"cmd": "interaction", "kind": "x"})
        error = await expect(owner_reader, "error")
        self.assertEqual(error["code"], "bad_request")
        await send(owner_writer, {"cmd": "ping"})
        self.assertEqual((await expect(owner_reader, "pong"))["type"], "pong")
        self.assertIn("DEPART4", self.state.rooms)
        guest_writer.close(); owner_writer.close()
        await guest_writer.wait_closed(); await owner_writer.wait_closed()

    def test_password_failure_table_is_bounded(self):
        for index in range(5000):
            self.state.lock_password_failure("10.0.0.1", f"ROOM{index}")
        self.assertLess(len(self.state.password_failures), 4097)
        for _ in range(8):
            self.state.lock_password_failure("10.0.0.2", "LOCKED")
        self.assertTrue(self.state.password_locked("10.0.0.2", "LOCKED"))
        for index in range(5000):
            self.state.lock_password_failure("10.0.0.3", f"SCAN{index}")
        self.assertTrue(self.state.password_locked("10.0.0.2", "LOCKED"))


class DecodeDepthTest(unittest.TestCase):
    @staticmethod
    def nested(levels):
        return (b"\x06" + struct.pack(">H", 1)) * levels + b"\x00"

    def test_reasonable_nesting_decodes(self):
        value, used = _decode_value(self.nested(MAX_DECODE_DEPTH))
        self.assertEqual(used, MAX_DECODE_DEPTH * 3 + 1)
        for _ in range(MAX_DECODE_DEPTH):
            value = value[0]
        self.assertIsNone(value)

    def test_hostile_nesting_is_a_plain_decode_error(self):
        with self.assertRaises(ValueError):
            _decode_value(self.nested(60000))


if __name__ == "__main__":
    unittest.main()
