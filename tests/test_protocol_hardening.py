import asyncio
import unittest

from test_server import receive, send


class ProtocolHardeningTest(unittest.IsolatedAsyncioTestCase):
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

    async def room(self):
        owner_reader, owner_writer, owner_id = await self.connect("Owner")
        await send(owner_writer, {"cmd": "create", "room": "HARD"})
        await receive(owner_reader)
        guest_reader, guest_writer, guest_id = await self.connect("Guest")
        await send(guest_writer, {"cmd": "join", "room": "HARD"})
        await receive(guest_reader)
        await receive(owner_reader)
        return owner_reader, owner_writer, owner_id, guest_reader, guest_writer, guest_id

    async def test_guest_drop_rejected(self):
        _, owner_writer, _, guest_reader, guest_writer, _ = await self.room()
        await send(guest_writer, {"cmd": "drop", "item_id": 1, "template": 12,
                                  "map": 1, "x": 1, "y": 1})
        error = await receive(guest_reader)
        self.assertEqual((error["type"], error["code"]),
                         ("error", "owner_world_only"))
        owner_writer.close()
        guest_writer.close()

    async def test_guest_clan_points_rejected(self):
        owner_reader, owner_writer, owner_id, guest_reader, guest_writer, guest_id = await self.room()
        await send(guest_writer, {"cmd": "interaction", "kind": "clan_points",
                                  "data": "99"})
        error = await receive(guest_reader)
        self.assertEqual(error["type"], "error")
        self.assertIn(error["code"], ("owner_world_only", "clan_required"))
        owner_writer.close()
        guest_writer.close()

    async def test_trade_accept_requires_invite(self):
        owner_reader, owner_writer, owner_id, guest_reader, guest_writer, guest_id = await self.room()
        room = self.state.rooms["HARD"]
        owner = room.players[owner_id]
        await send(guest_writer, {"cmd": "interaction", "kind": "trade_accept",
                                  "target_actor": owner.actor_id})
        error = await receive(guest_reader)
        self.assertEqual((error["type"], error["code"]),
                         ("error", "invite_required"))
        owner_writer.close()
        guest_writer.close()

    async def test_duplicate_drop_id_rejected(self):
        owner_reader, owner_writer, _, guest_reader, guest_writer, _ = await self.room()
        room = self.state.rooms["HARD"]
        for player in room.players.values():
            player.map_id = 1
            player.zone_id = 0
        await send(owner_writer, {"cmd": "drop", "item_id": 9, "template": 12,
                                  "map": 1, "zone": 0, "x": 1, "y": 1})
        first = await receive(owner_reader)
        self.assertEqual(first["type"], "drop_spawn")
        await receive(guest_reader)
        await send(owner_writer, {"cmd": "drop", "item_id": 9, "template": 12,
                                  "map": 1, "zone": 0, "x": 2, "y": 2})
        error = await receive(owner_reader)
        self.assertEqual((error["type"], error["code"]),
                         ("error", "drop_exists"))
        owner_writer.close()
        guest_writer.close()

    async def test_bad_int_is_bad_request(self):
        owner_reader, owner_writer, _, guest_reader, guest_writer, _ = await self.room()
        await send(owner_writer, {"cmd": "attack", "mob_id": "1",
                                  "damage": "nope", "hp": 1, "max_hp": 1})
        error = await receive(owner_reader)
        self.assertEqual((error["type"], error["code"]),
                         ("error", "bad_request"))
        owner_writer.close()
        guest_writer.close()

    async def test_hello_required_before_gameplay(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        await receive(reader)
        await send(writer, {"cmd": "attack", "mob_id": "1", "damage": 1,
                            "hp": 1, "max_hp": 1})
        error = await receive(reader)
        self.assertEqual((error["type"], error["code"]),
                         ("error", "hello_required"))
        writer.close()
        await writer.wait_closed()

    async def test_rate_limit_non_hot_path(self):
        from unittest.mock import patch
        reader, writer, _ = await self.connect("Limiter")
        with patch("nso_server.mixins.core.COMMANDS_PER_SEC", 3):
            limited = None
            for _ in range(8):
                await send(writer, {"cmd": "create", "room": "RL"})
                reply = await receive(reader)
                if reply.get("code") == "rate_limited":
                    limited = reply
                    break
            self.assertIsNotNone(limited)
            self.assertEqual((limited["type"], limited["code"]),
                             ("error", "rate_limited"))
        writer.close()
        await writer.wait_closed()
