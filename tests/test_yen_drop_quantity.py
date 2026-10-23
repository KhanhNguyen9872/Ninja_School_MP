import asyncio
import struct
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import RoomServer, REQUEST_OPCODES, EVENT_OPCODES, _encode_value, _decode_value

async def receive(reader):
    header = await asyncio.wait_for(reader.readexactly(8), timeout=4)
    assert header[:3] == b"NS\x02"
    value, consumed = _decode_value(await reader.readexactly(struct.unpack(">I", header[4:])[0]))
    value["type"] = next(name for name, code in EVENT_OPCODES.items() if code == header[3])
    return value

async def send(writer, command, **payload):
    opcode = next(code for code, name in REQUEST_OPCODES.items() if name == command)
    body = _encode_value(payload)
    writer.write(b"NS" + bytes((2, opcode)) + struct.pack(">I", len(body)) + body)
    await writer.drain()

class YenDropQuantityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = RoomServer()
        self.listener = await asyncio.start_server(self.state.handle, "127.0.0.1", 0)
        self.port = self.listener.sockets[0].getsockname()[1]
        self.writers = []

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.close()
        for writer in self.writers:
            await writer.wait_closed()
        self.listener.close()
        await self.listener.wait_closed()

    async def connect(self, name):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.writers.append(writer)
        welcome = await receive(reader)
        await send(writer, "hello", name=name)
        self.assertEqual((await receive(reader))["type"], "hello_ok")
        return reader, writer, welcome["player_id"]

    async def setup_room(self):
        ar, aw, aid = await self.connect("YenOwner")
        br, bw, bid = await self.connect("YenGuest")
        await send(aw, "create", room="YEN")
        self.assertEqual((await receive(ar))["type"], "room_joined")
        await send(bw, "join", room="YEN")
        self.assertEqual((await receive(br))["type"], "room_joined")
        self.assertEqual((await receive(ar))["type"], "player_join")
        return ar, aw, aid, br, bw, bid

    async def test_yen_amount_survives_spawn_snapshot_and_claim(self):
        ar, aw, aid, br, bw, bid = await self.setup_room()
        for item_id, amount in enumerate((50000, 1000000, 2147483647), 100):
            await send(aw, "drop", item_id=item_id, template=12, quantity=amount)
            a, event = await receive(ar), await receive(br)
            self.assertEqual(a["type"], "drop_spawn")
            drop = event["drop"]
            print(f"YEN_WIRE REQUEST={amount} SPAWN={drop['quantity']}", flush=True)
            self.assertEqual(drop["quantity"], amount)
            await send(bw, "snapshot")
            snapshot = await receive(br)
            rows = [d for d in snapshot["drops"] if d["drop_id"] == drop["drop_id"]]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["quantity"], amount)
            await send(bw, "pickup", drop_id=drop["drop_id"])
            self.assertEqual((await receive(br))["code"], "drop_owned")
            await send(aw, "pickup", drop_id=drop["drop_id"])
            self.assertEqual((await receive(ar))["type"], "drop_taken")
            self.assertEqual((await receive(br))["type"], "drop_taken")
            self.assertNotIn(drop["drop_id"], self.state.rooms["YEN"].drops)
            print(f"YEN_PICKUP REQUEST={amount} CACHED_AMOUNT={drop['quantity']} TAKEN_ONCE=True", flush=True)

    async def test_ordinary_stack_still_clamped_and_nonpositive_is_one(self):
        ar, aw, aid, br, bw, bid = await self.setup_room()
        for item_id, template, amount, expected in (
            (201, 42, 50000, 32767), (202, 12, 0, 1), (203, 12, -50, 1)
        ):
            await send(aw, "drop", item_id=item_id, template=template, quantity=amount)
            await receive(ar)
            event = await receive(br)
            self.assertEqual(event["type"], "drop_spawn")
            self.assertEqual(event["drop"]["quantity"], expected)

if __name__ == "__main__":
    unittest.main()
