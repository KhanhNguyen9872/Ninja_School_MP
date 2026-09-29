import asyncio
import hashlib
import json
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from server import (Room, RoomServer, REQUEST_OPCODES, EVENT_OPCODES,
                    _encode_value, _decode_value, password_digest)


async def receive(reader):
    header = await reader.readexactly(8)
    if header[:3] != b"NS\x02":
        raise AssertionError(f"invalid binary header {header!r}")
    size = struct.unpack(">I", header[4:])[0]
    payload, consumed = _decode_value(await reader.readexactly(size))
    if consumed != size or not isinstance(payload, dict):
        raise AssertionError("invalid binary payload")
    payload["type"] = next(name for name, opcode in EVENT_OPCODES.items()
                           if opcode == header[3])
    return payload


async def send(writer, payload):
    if isinstance(payload, str):
        payload = json.loads(payload)
    body = dict(payload)
    command = body.pop("cmd")
    opcode = next(code for code, name in REQUEST_OPCODES.items() if name == command)
    encoded = _encode_value(body)
    writer.write(b"NS" + bytes((2, opcode)) + struct.pack(">I", len(encoded)) + encoded)
    await writer.drain()


class RoomServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = RoomServer()
        self.server = await asyncio.start_server(self.state.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def connect(self, name):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        welcome = await receive(reader)
        await send(writer, {"cmd": "hello", "name": name})
        await writer.drain()
        self.assertEqual((await receive(reader))["type"], "hello_ok")
        return reader, writer, welcome["player_id"]

    async def test_create_join_state_and_chat(self):
        a_reader, a_writer, a_id = await self.connect("A")
        b_reader, b_writer, b_id = await self.connect("B")
        await send(a_writer, '{"cmd":"create","room":"TEST"}')
        await a_writer.drain()
        self.assertEqual((await receive(a_reader))["type"], "room_joined")
        await send(b_writer, '{"cmd":"join","room":"TEST"}')
        await b_writer.drain()
        self.assertEqual((await receive(b_reader))["type"], "room_joined")
        self.assertEqual((await receive(a_reader))["type"], "player_join")
        appearance = {"cmd": "appearance", "name": "NinjaA", "class_id": 1,
                      "gender": 0, "head": 11, "weapon": 22, "body": 33,
                      "leg": 44, "level": 66, "max_hp": 12345,
                      "fashion": [-1, 101, 102, 103, -1, -1, -1, -1, -1, -1],
                      "mount_ids": [-1, -1, -1, -1, 777],
                      "mount_upgrades": [-1, -1, -1, -1, 5],
                      "mount_systems": [-1, -1, -1, -1, 2],
                      "pet_template": 241, "pet_boss": True,
                      "bijuu_template": 266, "bijuu_boss": True}
        await send(a_writer, appearance)
        await a_writer.drain()
        projected = await receive(b_reader)
        self.assertEqual(projected["type"], "player_appearance")
        self.assertEqual(projected["appearance"]["name"], "NinjaA")
        self.assertEqual(projected["appearance"]["fashion"][1], 101)
        self.assertGreater(projected["actor_id"], 0)
        await send(b_writer, {"cmd": "appearance", "name": "NinjaB"})
        await b_writer.drain()
        self.assertEqual((await receive(a_reader))["type"], "player_appearance")
        await send(a_writer, {"cmd": "private_chat", "target": "NinjaB",
                                    "text": "hello private"})
        await a_writer.drain()
        private = await receive(b_reader)
        self.assertEqual((private["type"], private["name"], private["text"]),
                         ("private_chat", "NinjaA", "hello private"))
        await send(a_writer, '{"cmd":"state","map":2,"x":10,"y":20,"hp":99}')
        await a_writer.drain()
        state = await receive(b_reader)
        self.assertEqual(state["type"], "player_state")
        self.assertEqual((state["map"], state["x"], state["y"]), (2, 10, 20))
        await send(a_writer, {"cmd": "state", "map": 2, "x": 10, "y": 20, "hp": 0})
        exhausted = await receive(b_reader)
        self.assertEqual((exhausted["type"], exhausted["hp"], exhausted["player_id"]),
                         ("player_state", 0, a_id))
        self.assertEqual(self.state.rooms["TEST"].players[a_id].hp, 0)
        await send(a_writer, {"cmd": "state", "map": 2, "x": 10, "y": 20, "hp": 99})
        self.assertEqual((await receive(b_reader))["hp"], 99)
        await send(b_writer, '{"cmd":"snapshot"}')
        await b_writer.drain()
        snapshot = await receive(b_reader)
        self.assertEqual(snapshot["type"], "snapshot")
        self.assertEqual(len(snapshot["players"]), 2)
        owner = [row for row in snapshot["players"] if row["name"] == "NinjaA"][0]
        self.assertEqual(owner["appearance"]["pet_template"], 241)
        await send(a_writer, '{"cmd":"chat","text":"hello"}')
        await a_writer.drain()
        own_world = await receive(a_reader)
        peer_world = await receive(b_reader)
        self.assertEqual((own_world["type"], own_world["channel"], own_world["name"],
                          own_world["text"]), ("chat", "world", "NinjaA", "hello"))
        self.assertEqual((peer_world["channel"], peer_world["actor_id"],
                          peer_world["text"]), ("world", projected["actor_id"], "hello"))
        await send(a_writer, '{"cmd":"map","map":3,"x":40,"y":50}')
        await a_writer.drain()
        transition = await receive(b_reader)
        self.assertEqual(transition["type"], "map_transition")
        self.assertEqual(transition["map"], 3)
        await send(b_writer, {"cmd": "map", "map": 9, "x": 12, "y": 13})
        guest_transition = await receive(a_reader)
        self.assertEqual((guest_transition["type"], guest_transition["map"]),
                         ("map_transition", 9))
        room = self.state.rooms["TEST"]
        self.assertEqual(room.players[a_id].map_id, 3)
        self.assertEqual(room.players[b_id].map_id, 9)
        await send(a_writer, {"cmd": "chat", "channel": "map",
                              "text": "hello map 3"})
        own_map_chat = await receive(a_reader)
        self.assertEqual((own_map_chat["type"], own_map_chat["channel"],
                          own_map_chat["map"], own_map_chat["text"]),
                         ("chat", "map", 3, "hello map 3"))
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(receive(b_reader), 0.05)
        await send(b_writer, {"cmd": "map", "map": 3, "x": 42, "y": 50})
        self.assertEqual((await receive(a_reader))["map"], 3)
        await send(b_writer, {"cmd": "chat", "channel": "map",
                              "text": "now together"})
        a_map_chat = await receive(a_reader)
        b_map_chat = await receive(b_reader)
        self.assertEqual((a_map_chat["channel"], a_map_chat["name"],
                          a_map_chat["text"]), ("map", "NinjaB", "now together"))
        self.assertEqual((b_map_chat["channel"], b_map_chat["map"]), ("map", 3))
        await send(b_writer, {"cmd": "state", "map": 3, "x": 42, "y": 50, "hp": 100})
        await b_writer.drain()
        self.assertEqual((await receive(a_reader))["type"], "player_state")
        await send(a_writer, '{"cmd":"attack","mob_id":"mob-1","damage":100,"drop_template":42}')
        await a_writer.drain()
        mob_event = await receive(b_reader)
        await send(a_writer, {"cmd": "drop", "item_id": 55, "template": 42,
                              "map": 3, "x": 40, "y": 50, "quantity": 2,
                              "locked": False, "upgrade": 4, "sys_up": 2,
                              "expire": -1, "data": "6,10;7,20"})
        await a_writer.drain()
        drop_event = await receive(b_reader)
        self.assertEqual(mob_event["type"], "mob_state")
        self.assertEqual(drop_event["type"], "drop_spawn")
        self.assertEqual((drop_event["drop"]["quantity"], drop_event["drop"]["upgrade"],
                          drop_event["drop"]["data"]), (2, 4, "6,10;7,20"))
        await send(b_writer, {"cmd": "pickup", "drop_id": drop_event["drop"]["drop_id"]})
        await b_writer.drain()
        protected = await receive(b_reader)
        self.assertEqual((protected["type"], protected["code"]),
                         ("error", "drop_owned"))
        self.state.rooms["TEST"].drops[drop_event["drop"]["drop_id"]]["protected_until"] = 0
        await send(b_writer, {"cmd": "pickup", "drop_id": drop_event["drop"]["drop_id"]})
        claimed = await receive(b_reader)
        self.assertEqual((claimed["type"], claimed["player_id"]),
                         ("drop_taken", b_id))
        owner_events = [await receive(a_reader), await receive(a_reader), await receive(a_reader)]
        self.assertIn("drop_taken", [event["type"] for event in owner_events])
        self.assertNotIn(drop_event["drop"]["drop_id"], self.state.rooms["TEST"].drops)
        a_writer.close(); b_writer.close()
        await a_writer.wait_closed(); await b_writer.wait_closed()

    async def test_party_roster_refreshes_all_existing_members(self):
        a_reader, a_writer, a_id = await self.connect("Leader")
        b_reader, b_writer, b_id = await self.connect("MemberB")
        c_reader, c_writer, c_id = await self.connect("MemberC")
        await send(a_writer, {"cmd": "create", "room": "ROSTER"})
        await receive(a_reader)
        await send(b_writer, {"cmd": "join", "room": "ROSTER"})
        await receive(b_reader); await receive(a_reader)
        await send(c_writer, {"cmd": "join", "room": "ROSTER"})
        await receive(c_reader); await receive(a_reader); await receive(b_reader)
        room = self.state.rooms["ROSTER"]
        a_actor = room.players[a_id].actor_id
        b_actor = room.players[b_id].actor_id
        c_actor = room.players[c_id].actor_id
        await send(a_writer, {"cmd": "interaction", "kind": "party_invite",
                              "target_actor": b_actor})
        await receive(b_reader)
        await send(b_writer, {"cmd": "interaction", "kind": "party_accept",
                              "target_actor": a_actor})
        first_a, first_b = await asyncio.gather(receive(a_reader), receive(b_reader))
        self.assertEqual(len(first_a["members"]), 2)
        self.assertEqual(len(first_b["members"]), 2)
        await send(a_writer, {"cmd": "interaction", "kind": "party_invite",
                              "target_actor": c_actor})
        await receive(c_reader)
        await send(c_writer, {"cmd": "interaction", "kind": "party_accept",
                              "target_actor": a_actor})
        refreshed = await asyncio.gather(receive(a_reader), receive(b_reader),
                                         receive(c_reader))
        expected = {a_id, b_id, c_id}
        for roster in refreshed:
            self.assertEqual(roster["type"], "party_roster")
            self.assertEqual({row["player_id"] for row in roster["members"]}, expected)
        a_writer.close(); b_writer.close(); c_writer.close()
        await asyncio.gather(a_writer.wait_closed(), b_writer.wait_closed(),
                             c_writer.wait_closed())

    async def test_password_protected_room_rejects_wrong_and_accepts_correct(self):
        a_reader, a_writer, _ = await self.connect("Owner")
        b_reader, b_writer, _ = await self.connect("Friend")
        await send(a_writer, '{"cmd":"create","room":"LOCKED","password":"secret"}')
        await a_writer.drain()
        self.assertEqual((await receive(a_reader))["type"], "room_joined")
        await send(b_writer, '{"cmd":"join","room":"LOCKED","password":"wrong"}')
        await b_writer.drain()
        self.assertEqual((await receive(b_reader))["code"], "wrong_password")
        await send(b_writer, '{"cmd":"join","room":"LOCKED","password":"secret"}')
        await b_writer.drain()
        self.assertEqual((await receive(b_reader))["type"], "room_joined")
        self.assertEqual((await receive(a_reader))["type"], "player_join")
        self.assertNotEqual(self.state.rooms["LOCKED"].password_hash, "secret")
        a_writer.close(); b_writer.close()
        await a_writer.wait_closed(); await b_writer.wait_closed()

    async def test_ping_reports_owner_responsiveness_to_guest(self):
        owner_reader, owner_writer, owner_id = await self.connect("Owner")
        guest_reader, guest_writer, _ = await self.connect("Guest")
        await send(owner_writer, {"cmd": "create", "room": "HEARTBEAT"})
        self.assertEqual((await receive(owner_reader))["type"], "room_joined")
        await send(guest_writer, {"cmd": "join", "room": "HEARTBEAT"})
        self.assertEqual((await receive(guest_reader))["type"], "room_joined")
        self.assertEqual((await receive(owner_reader))["type"], "player_join")

        room = self.state.rooms["HEARTBEAT"]
        room.players[owner_id].last_seen = time.monotonic() - 30.0
        await send(guest_writer, {"cmd": "ping"})
        stalled = await receive(guest_reader)
        self.assertEqual(stalled["type"], "pong")
        self.assertFalse(stalled["owner_responsive"])
        self.assertGreaterEqual(stalled["owner_lag_ms"], 6000)

        await send(owner_writer, {"cmd": "ping"})
        owner_pong = await receive(owner_reader)
        self.assertTrue(owner_pong["owner_responsive"])
        await send(guest_writer, {"cmd": "ping"})
        resumed = await receive(guest_reader)
        self.assertTrue(resumed["owner_responsive"])
        self.assertLess(resumed["owner_lag_ms"], 6000)
        owner_writer.close(); guest_writer.close()
        await owner_writer.wait_closed(); await guest_writer.wait_closed()

    async def test_state_dedup_and_room_wide_mob_drop_projection(self):
        a_reader, a_writer, a_id = await self.connect("Attacker")
        b_reader, b_writer, _ = await self.connect("WatcherB")
        c_reader, c_writer, _ = await self.connect("WatcherC")
        await send(a_writer, {"cmd": "create", "room": "MOBSYNC"}); await receive(a_reader)
        await send(b_writer, {"cmd": "join", "room": "MOBSYNC"})
        await receive(b_reader); await receive(a_reader)
        await send(c_writer, {"cmd": "join", "room": "MOBSYNC"})
        await receive(c_reader); await receive(a_reader); await receive(b_reader)
        for writer, others in ((a_writer, (b_reader, c_reader)),
                               (b_writer, (a_reader, c_reader)),
                               (c_writer, (a_reader, b_reader))):
            await send(writer, {"cmd": "state", "map": 22, "x": 10, "y": 20, "hp": 100})
            for reader in others: self.assertEqual((await receive(reader))["type"], "player_state")
        await send(a_writer, {"cmd": "state", "map": 22, "x": 10, "y": 20, "hp": 100})
        for reader in (b_reader, c_reader):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(receive(reader), 0.05)

        await send(a_writer, {"cmd": "attack", "mob_id": "5", "damage": 30,
                              "hp": 70, "max_hp": 100, "drop_template": 42,
                              "skill": 7})
        for reader in (a_reader, b_reader, c_reader):
            hit = await receive(reader)
            self.assertEqual((hit["type"], hit["mob"]["hp"], hit["mob"]["alive"]),
                             ("mob_state", 70, True))
            self.assertEqual((hit["skill"], hit["attacker_actor_id"]),
                             (7, self.state.rooms["MOBSYNC"].players[a_id].actor_id))
        await send(a_writer, {"cmd": "attack", "mob_id": "5", "damage": 70,
                              "hp": 0, "max_hp": 100, "drop_template": 42})
        for reader in (a_reader, b_reader, c_reader):
            death = await receive(reader)
            self.assertEqual((death["type"], death["mob"]["hp"], death["mob"]["alive"]),
                             ("mob_state", 0, False))
        await send(a_writer, {"cmd": "drop", "item_id": 77, "template": 42,
                              "map": 22, "x": 10, "y": 20})
        drop_ids = []
        for reader in (a_reader, b_reader, c_reader):
            drop = await receive(reader)
            self.assertEqual((drop["type"], drop["drop"]["template"]),
                             ("drop_spawn", 42))
            drop_ids.append(drop["drop"]["drop_id"])
        self.assertEqual(len(set(drop_ids)), 1)
        for writer in (a_writer, b_writer, c_writer): writer.close()
        for writer in (a_writer, b_writer, c_writer): await writer.wait_closed()

    async def test_party_support_buff_reaches_party_and_projects_to_same_map(self):
        a_reader, a_writer, a_id = await self.connect("Buffer")
        b_reader, b_writer, b_id = await self.connect("Member")
        c_reader, c_writer, _ = await self.connect("Watcher")
        await send(a_writer, {"cmd": "create", "room": "BUFFS"}); await receive(a_reader)
        await send(b_writer, {"cmd": "join", "room": "BUFFS"})
        await receive(b_reader); await receive(a_reader)
        await send(c_writer, {"cmd": "join", "room": "BUFFS"})
        await receive(c_reader); await receive(a_reader); await receive(b_reader)
        for writer, others, x in ((a_writer, (b_reader, c_reader), 10),
                                  (b_writer, (a_reader, c_reader), 20),
                                  (c_writer, (a_reader, b_reader), 30)):
            await send(writer, {"cmd": "state", "map": 22, "x": x, "y": 10, "hp": 100})
            for reader in others: self.assertEqual((await receive(reader))["type"], "player_state")
        room = self.state.rooms["BUFFS"]
        await send(a_writer, {"cmd": "interaction", "kind": "party_invite",
                              "target_actor": room.players[b_id].actor_id})
        self.assertEqual((await receive(b_reader))["type"], "party_invite")
        await send(b_writer, {"cmd": "interaction", "kind": "party_accept",
                              "target_actor": room.players[a_id].actor_id})
        self.assertEqual((await receive(a_reader))["type"], "party_roster")
        self.assertEqual((await receive(b_reader))["type"], "party_roster")
        await send(a_writer, {"cmd": "interaction", "kind": "party_buff", "skill": 51,
                              "target_actor": 0, "data": "19,90000,60,200,300,200"})
        for reader in (a_reader, b_reader, c_reader):
            events = [await receive(reader), await receive(reader)]
            self.assertEqual([event["type"] for event in events], ["party_buff", "party_buff"])
            self.assertEqual({event["target_id"] for event in events}, {a_id, b_id})
            self.assertTrue(all(event["skill"] == 51 and event["template"] == 19
                                for event in events))
        a_writer.close(); b_writer.close(); c_writer.close()
        await a_writer.wait_closed(); await b_writer.wait_closed(); await c_writer.wait_closed()

    async def test_duplicate_local_clan_names_receive_room_aliases(self):
        a_reader, a_writer, a_id = await self.connect("ClanA")
        b_reader, b_writer, b_id = await self.connect("ClanB")
        await send(a_writer, {"cmd": "create", "room": "CLANNAMES"}); await receive(a_reader)
        await send(b_writer, {"cmd": "join", "room": "CLANNAMES"})
        await receive(b_reader); await receive(a_reader)
        await send(a_writer, {"cmd": "appearance", "name": "ClanA", "clan_name": "Leaf"})
        first_seen = await receive(b_reader)
        self.assertEqual(first_seen["appearance"]["clan_name"], "Leaf")
        self.assertEqual((await receive(a_reader))["type"], "clan_state")
        await send(b_writer, {"cmd": "appearance", "name": "ClanB", "clan_name": "Leaf"})
        second_seen = await receive(a_reader)
        self.assertEqual(second_seen["appearance"]["clan_name"], "Leaf_1")
        assigned = await receive(b_reader)
        self.assertEqual((assigned["type"], assigned["clan_name"]),
                         ("clan_name_assigned", "Leaf_1"))
        self.assertEqual((await receive(b_reader))["type"], "clan_state")
        room = self.state.rooms["CLANNAMES"]
        self.assertEqual(room.players[a_id].clan_name, "Leaf")
        self.assertEqual(room.players[b_id].clan_name, "Leaf_1")
        self.assertIn("leaf", room.clans); self.assertIn("leaf_1", room.clans)
        a_writer.close(); b_writer.close()
        await a_writer.wait_closed(); await b_writer.wait_closed()

    async def test_room_rejects_cheat_mismatch_and_selected_character_change(self):
        owner_reader, owner_writer, _ = await self.connect("Owner")
        guest_reader, guest_writer, _ = await self.connect("Guest")
        await send(owner_writer, {"cmd": "create", "room": "FAIR",
                                  "cheat_enabled": False})
        self.assertEqual((await receive(owner_reader))["type"], "room_joined")
        await send(guest_writer, {"cmd": "join", "room": "FAIR",
                                  "cheat_enabled": True})
        mismatch = await receive(guest_reader)
        self.assertEqual((mismatch["type"], mismatch["code"]),
                         ("error", "cheat_mismatch"))
        await send(guest_writer, {"cmd": "join", "room": "FAIR",
                                  "cheat_enabled": False})
        self.assertEqual((await receive(guest_reader))["type"], "room_joined")
        self.assertEqual((await receive(owner_reader))["type"], "player_join")
        await send(guest_writer, {"cmd": "appearance", "name": "GuestCheat",
                                  "cheat_enabled": True})
        selected_mismatch = await receive(guest_reader)
        self.assertEqual(selected_mismatch["code"], "cheat_mismatch")
        owner_writer.close(); guest_writer.close()
        await owner_writer.wait_closed(); await guest_writer.wait_closed()

    async def test_unique_character_names_include_owner_world_reserved_names(self):
        owner_reader, owner_writer, owner_id = await self.connect("Owner")
        first_reader, first_writer, first_id = await self.connect("First")
        second_reader, second_writer, second_id = await self.connect("Second")
        await send(owner_writer, {"cmd": "create", "room": "NAMES"})
        await receive(owner_reader)
        await send(first_writer, {"cmd": "join", "room": "NAMES"})
        await receive(first_reader); await receive(owner_reader)
        await send(second_writer, {"cmd": "join", "room": "NAMES"})
        await receive(second_reader); await receive(owner_reader); await receive(first_reader)
        await send(owner_writer, {"cmd": "appearance", "name": "OwnerNinja",
                                  "reserved_names": "admin\nmerchant"})
        self.assertEqual((await receive(first_reader))["type"], "player_appearance")
        self.assertEqual((await receive(second_reader))["type"], "player_appearance")
        await send(first_writer, {"cmd": "appearance", "name": "admin"})
        assigned_first = await receive(first_reader)
        self.assertEqual((assigned_first["type"], assigned_first["name"]),
                         ("name_assigned", "admin_1"))
        await receive(owner_reader); await receive(second_reader)
        await send(first_writer, {"cmd": "appearance", "name": "admin_1"})
        self.assertEqual((await receive(owner_reader))["appearance"]["name"], "admin_1")
        self.assertEqual((await receive(second_reader))["appearance"]["name"], "admin_1")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(receive(first_reader), 0.25)
        await send(second_writer, {"cmd": "appearance", "name": "ADMIN"})
        assigned_second = await receive(second_reader)
        self.assertEqual((assigned_second["type"], assigned_second["name"]),
                         ("name_assigned", "ADMIN_2"))
        room = self.state.rooms["NAMES"]
        self.assertEqual(room.players[first_id].name, "admin_1")
        self.assertEqual(room.players[second_id].name, "ADMIN_2")
        self.assertEqual(room.players[owner_id].name, "OwnerNinja")
        owner_writer.close(); first_writer.close(); second_writer.close()
        await owner_writer.wait_closed(); await first_writer.wait_closed(); await second_writer.wait_closed()

    async def test_friend_clan_and_cuu_sat_lifecycle(self):
        a_reader, a_writer, a_id = await self.connect("Alpha")
        b_reader, b_writer, b_id = await self.connect("Beta")
        await send(a_writer, {"cmd": "create", "room": "RELATIONS"})
        await receive(a_reader)
        await send(b_writer, {"cmd": "join", "room": "RELATIONS"})
        await receive(b_reader); await receive(a_reader)
        room = self.state.rooms["RELATIONS"]
        a_actor = room.players[a_id].actor_id; b_actor = room.players[b_id].actor_id
        room.players[a_id].hp = 100; room.players[b_id].hp = 100
        room.players[a_id].appearance = {"damage": 35}
        room.players[b_id].appearance = {"damage_down": 0}

        await send(a_writer, {"cmd": "interaction", "kind": "friend_invite",
                              "target_actor": b_actor})
        self.assertEqual((await receive(a_reader))["relation_type"], 0)
        self.assertEqual((await receive(b_reader))["type"], "friend_invite")
        await send(b_writer, {"cmd": "interaction", "kind": "friend_invite",
                              "target_actor": a_actor})
        self.assertEqual((await receive(a_reader))["type"], "friend_add")
        self.assertEqual((await receive(b_reader))["type"], "friend_add")
        self.assertIn(b_id, room.friends[a_id]); self.assertIn(a_id, room.friends[b_id])

        await send(a_writer, {"cmd": "interaction", "kind": "clan_invite",
                              "target_actor": b_actor, "data": "Konoha"})
        invite = await receive(b_reader)
        self.assertEqual((invite["type"], invite["clan_name"]), ("clan_invite", "Konoha"))
        await send(b_writer, {"cmd": "interaction", "kind": "clan_accept",
                              "target_actor": a_actor})
        self.assertEqual((await receive(a_reader))["type"], "clan_joined")
        self.assertEqual((await receive(b_reader))["clan_name"], "Konoha")
        self.assertEqual((await receive(a_reader))["type"], "clan_state")
        self.assertEqual((await receive(b_reader))["type"], "clan_state")
        await send(a_writer, {"cmd": "interaction", "kind": "clan_chat",
                              "target_actor": 0, "data": "xin chao"})
        self.assertEqual((await receive(a_reader))["text"], "xin chao")
        self.assertEqual((await receive(b_reader))["type"], "clan_chat")

        await send(a_writer, {"cmd": "interaction", "kind": "cuu_sat",
                              "target_actor": b_actor})
        self.assertEqual((await receive(a_reader))["type"], "cuu_sat_start")
        self.assertEqual((await receive(b_reader))["type"], "cuu_sat_start")
        room.players[b_id].hp = 100
        await send(a_writer, {"cmd": "interaction", "kind": "pvp_attack",
                              "target_actor": b_actor, "damage": 35, "skill": 4})
        nonfatal = await receive(a_reader)
        self.assertEqual((nonfatal["hp"], nonfatal["attacker"], nonfatal["amount"]),
                         (65, "Alpha", 0))
        self.assertEqual((await receive(b_reader))["type"], "pvp_hit")
        await send(a_writer, {"cmd": "interaction", "kind": "cuu_sat_clear",
                              "target_actor": 0})
        self.assertEqual((await receive(a_reader))["type"], "cuu_sat_end")
        self.assertEqual((await receive(b_reader))["type"], "cuu_sat_end")
        a_writer.close(); b_writer.close()
        await a_writer.wait_closed(); await b_writer.wait_closed()

    async def test_cuu_sat_ends_for_both_when_either_player_changes_map(self):
        a_reader, a_writer, a_id = await self.connect("MapAggressor")
        b_reader, b_writer, b_id = await self.connect("MapTarget")
        await send(a_writer, {"cmd": "create", "room": "PKMAP"})
        await receive(a_reader)
        await send(b_writer, {"cmd": "join", "room": "PKMAP"})
        await receive(b_reader); await receive(a_reader)
        room = self.state.rooms["PKMAP"]
        a_actor = room.players[a_id].actor_id
        b_actor = room.players[b_id].actor_id
        await send(a_writer, {"cmd": "interaction", "kind": "cuu_sat",
                              "target_actor": b_actor})
        self.assertEqual((await receive(a_reader))["type"], "cuu_sat_start")
        self.assertEqual((await receive(b_reader))["type"], "cuu_sat_start")
        self.assertEqual(room.cuu_sat, {a_id: b_id})

        await send(a_writer, {"cmd": "map", "map": 22, "x": 228, "y": 192})
        ended_a = await asyncio.wait_for(receive(a_reader), 1.0)
        ended_b = await asyncio.wait_for(receive(b_reader), 1.0)
        self.assertEqual((ended_a["type"], ended_a.get("reason")),
                         ("cuu_sat_end", "map_transition"))
        self.assertEqual((ended_b["type"], ended_b.get("reason")),
                         ("cuu_sat_end", "map_transition"))
        self.assertEqual((await receive(b_reader))["type"], "map_transition")
        self.assertNotIn(a_id, room.cuu_sat)
        self.assertNotIn(b_id, room.cuu_sat)
        self.assertEqual(room.players[a_id].map_id, 22)

        await send(b_writer, {"cmd": "map", "map": 22, "x": 230, "y": 192})
        self.assertEqual((await receive(a_reader))["type"], "map_transition")
        await send(a_writer, {"cmd": "interaction", "kind": "pvp_attack",
                              "target_actor": b_actor, "damage": 35, "skill": 4})
        blocked = await receive(a_reader)
        self.assertEqual((blocked["type"], blocked["code"]),
                         ("error", "cuu_sat_not_open"))

        await send(a_writer, {"cmd": "interaction", "kind": "cuu_sat",
                              "target_actor": b_actor})
        self.assertEqual((await receive(a_reader))["type"], "cuu_sat_start")
        self.assertEqual((await receive(b_reader))["type"], "cuu_sat_start")
        await send(b_writer, {"cmd": "map", "map": 6, "x": 37, "y": 120})
        self.assertEqual((await receive(b_reader))["type"], "cuu_sat_end")
        self.assertEqual((await receive(a_reader))["type"], "cuu_sat_end")
        self.assertEqual((await receive(a_reader))["type"], "map_transition")
        self.assertEqual(room.cuu_sat, {})
        self.assertEqual(room.players[b_id].map_id, 6)
        await send(b_writer, {"cmd": "interaction", "kind": "pvp_attack",
                              "target_actor": a_actor, "damage": 35, "skill": 4})
        blocked_other = await receive(b_reader)
        self.assertEqual((blocked_other["type"], blocked_other["code"]),
                         ("error", "interaction_wrong_map"))
        a_writer.close(); b_writer.close()
        await a_writer.wait_closed(); await b_writer.wait_closed()

    async def test_shared_clan_roles_fund_upgrade_pet_territory_and_war(self):
        clients = [await self.connect(name) for name in ("Leaf", "LeafMember", "Sand", "SandMember")]
        a_reader, a_writer, a_id = clients[0]
        await send(a_writer, {"cmd": "create", "room": "CLANFULL"}); await receive(a_reader)
        for reader, writer, _ in clients[1:]:
            await send(writer, {"cmd": "join", "room": "CLANFULL"}); await receive(reader)
        room = self.state.rooms["CLANFULL"]
        a, b, c, d = [room.players[player_id] for _, _, player_id in clients]
        for player, name, level in ((a, "Leaf", 60), (b, "LeafMember", 60),
                                    (c, "Sand", 60), (d, "SandMember", 60)):
            player.name = name; player.appearance = {"name": name, "level": level, "class_id": 1}
        leaf = self.state.ensure_clan(room, a, "Konoha")
        sand = self.state.ensure_clan(room, c, "Suna")

        await self.state.interaction(room, a, {"kind": "clan_invite",
                                               "target_actor": b.actor_id, "data": "Konoha"})
        await self.state.interaction(room, b, {"kind": "clan_accept",
                                               "target_actor": a.actor_id})
        self.assertIn(b.player_id, leaf["members"])
        await self.state.interaction(room, a, {"kind": "clan_role",
                                               "target_actor": b.actor_id, "data": "3"})
        self.assertEqual(leaf["members"][b.player_id]["role"], 3)
        await self.state.interaction(room, b, {"kind": "clan_contribute",
                                               "target_actor": 0, "data": "600000"})
        await self.state.interaction(room, b, {"kind": "clan_points",
                                               "target_actor": 0, "data": "2000"})
        self.assertEqual((leaf["coin"], leaf["exp"], leaf["members"][b.player_id]["points"]),
                         (600000, 2000, 2000))
        await self.state.interaction(room, a, {"kind": "clan_upgrade",
                                               "target_actor": 0, "data": ""})
        self.assertEqual((leaf["level"], leaf["coin"], leaf["exp"]), (2, 0, 0))
        leaf["level"] = 5; leaf["coin"] = 1000000
        await self.state.interaction(room, a, {"kind": "clan_item_upgrade",
                                               "target_actor": 0, "data": ""})
        self.assertEqual((leaf["item_level"], leaf["coin"]), (1, 0))
        deadline = int(time.time() * 1000) + 3600000
        await self.state.interaction(room, a, {"kind": "clan_pet",
                                               "target_actor": 0,
                                               "data": f"0,{deadline},0"})
        self.assertEqual(leaf["pets"][0], deadline)
        await self.state.interaction(room, b, {"kind": "clan_pet_sync",
                                               "target_actor": 0,
                                               "data": "0,-9223372036854775000"})
        self.assertEqual(leaf["pets"][0], -9223372036854775000)
        await self.state.interaction(room, a, {"kind": "clan_territory_open",
                                               "target_actor": 0, "data": ""})
        self.assertTrue(leaf["territory"]["active"])

        await self.state.interaction(room, c, {"kind": "clan_invite",
                                               "target_actor": d.actor_id, "data": "Suna"})
        await self.state.interaction(room, d, {"kind": "clan_accept",
                                               "target_actor": c.actor_id})
        await self.state.interaction(room, a, {"kind": "clan_war_invite",
                                               "target_actor": c.actor_id, "data": ""})
        self.assertEqual(sand["war"]["pending_from"], "Konoha")
        await self.state.interaction(room, c, {"kind": "clan_war_accept",
                                               "target_actor": 0, "data": ""})
        self.assertTrue(leaf["war"]["active"]); self.assertTrue(sand["war"]["active"])
        await self.state.interaction(room, b, {"kind": "clan_war_points",
                                               "target_actor": 0, "data": "25"})
        self.assertEqual((leaf["war"]["own_points"], sand["war"]["enemy_points"]),
                         (25, 25))
        payload = self.state.clan_payload(room, leaf)
        self.assertEqual((payload["clan_name"], payload["owner_id"], payload["category"]),
                         ("Konoha", a.player_id, 1))
        for _, writer, _ in clients: writer.close()
        for _, writer, _ in clients: await writer.wait_closed()

    async def test_shared_clan_warehouse_purchase_delivery_and_entry_card(self):
        a_reader, a_writer, a_id = await self.connect("StoreChief")
        b_reader, b_writer, b_id = await self.connect("StoreMember")
        await send(a_writer, {"cmd": "create", "room": "CLANSTORE"}); await receive(a_reader)
        await send(b_writer, {"cmd": "join", "room": "CLANSTORE"})
        await receive(b_reader); await receive(a_reader)
        room = self.state.rooms["CLANSTORE"]
        a, b = room.players[a_id], room.players[b_id]
        a.name = "StoreChief"; b.name = "StoreMember"
        a.appearance = {"name": a.name, "level": 60, "class_id": 1}
        b.appearance = {"name": b.name, "level": 60, "class_id": 1}
        clan = self.state.ensure_clan(room, a, "Stock")
        # B is an invited member of A's clan, not a second locally-created
        # clan with the same requested name (which is correctly aliased).
        clan["members"][b.player_id] = {"name": b.name, "class_id": 1,
                                          "level": 60, "role": 0, "points": 0}
        b.clan_name = str(clan["name"])
        clan["coin"] = 10000

        await self.state.interaction(room, a, {"kind": "clan_store_buy",
                                               "target_actor": 0,
                                               "data": "500,2,1000,0,0,1,-1,1=10"})
        bought_a = await receive(a_reader); bought_b = await receive(b_reader)
        self.assertIn("500,2,0,0,1,-1,1=10", bought_a["inventory"])
        self.assertEqual(bought_b["amount"], 8000)

        await self.state.interaction(room, b, {"kind": "clan_store_buy",
                                               "target_actor": 0,
                                               "data": "501,1,100,0,0,0,-1,"})
        self.assertEqual((await receive(b_reader))["code"], "clan_role_denied")

        await self.state.interaction(room, a, {"kind": "clan_item_send",
                                               "target_actor": b.actor_id,
                                               "data": "500"})
        delivered = await receive(b_reader)
        self.assertEqual((delivered["type"], delivered["data"]),
                         ("clan_item_delivery", "500,1,0,0,1,-1,1=10"))
        state_a = await receive(a_reader); state_b = await receive(b_reader)
        self.assertIn("500,1,0,0,1,-1,1=10", state_a["inventory"])
        self.assertEqual(state_b["type"], "clan_state")
        self.assertIn(delivered["delivery_id"], room.pending_deliveries[b_id])

        clan["items"]["281"] = {"item": 281, "quantity": 1, "locked": False,
                                  "upgrade": 0, "sys": 0, "expire": -1, "options": ""}
        clan["territory"].update({"active": False, "entries": 0, "use_card": 1})
        await self.state.interaction(room, b, {"kind": "clan_item_use",
                                               "target_actor": 0, "data": "281"})
        used_a = await receive(a_reader); used_b = await receive(b_reader)
        self.assertEqual(used_a["payload"]["territory"]["entries"], 1)
        self.assertEqual(used_b["payload"]["territory"]["use_card"], 0)
        self.assertNotIn("281", clan["items"])
        a_writer.close(); b_writer.close()
        await a_writer.wait_closed(); await b_writer.wait_closed()

    async def test_owner_admin_lists_kicks_and_rotates_room_password(self):
        owner_reader, owner_writer, owner_id = await self.connect("Owner")
        guest_reader, guest_writer, guest_id = await self.connect("Guest")
        await send(owner_writer, {"cmd": "create", "room": "ADMIN", "password": "old"})
        owner_joined = await receive(owner_reader)
        await send(guest_writer, {"cmd": "join", "room": "ADMIN", "password": "old"})
        guest_joined = await receive(guest_reader); await receive(owner_reader)
        room = self.state.rooms["ADMIN"]
        owner_actor = room.players[owner_id].actor_id
        guest_actor = room.players[guest_id].actor_id

        await send(guest_writer, {"cmd": "interaction", "kind": "room_password",
                                  "target_actor": 0, "data": "forged"})
        denied = await receive(guest_reader)
        self.assertEqual((denied["type"], denied["code"]), ("error", "owner_room_only"))

        await send(owner_writer, {"cmd": "interaction", "kind": "room_password",
                                  "target_actor": 0, "data": "new"})
        changed = await receive(owner_reader)
        self.assertEqual((changed["type"], changed["password"]),
                         ("room_password_changed", True))
        self.assertEqual(room.password_hash, password_digest("new"))
        # Existing member remains live without re-authentication.
        await send(guest_writer, {"cmd": "state", "map": 1, "x": 8, "y": 9, "hp": 10})
        self.assertEqual((await receive(owner_reader))["type"], "player_state")

        late_reader, late_writer, _ = await self.connect("Late")
        await send(late_writer, {"cmd": "join", "room": "ADMIN", "password": "old"})
        self.assertEqual((await receive(late_reader))["code"], "wrong_password")
        await send(late_writer, {"cmd": "join", "room": "ADMIN", "password": "new"})
        late_joined = await receive(late_reader); await receive(owner_reader); await receive(guest_reader)
        self.assertEqual(late_joined["type"], "room_joined")

        # Password rotation invalidates the old guest resume credential.
        guest_writer.close(); await guest_writer.wait_closed()
        for _ in range(20):
            if guest_id not in self.state.players: break
            await asyncio.sleep(0.01)
        owner_guest_left = await receive(owner_reader)
        late_guest_left = await receive(late_reader)
        self.assertEqual((owner_guest_left["type"], owner_guest_left["player_id"]),
                         ("player_leave", guest_id))
        self.assertEqual((late_guest_left["type"], late_guest_left["player_id"]),
                         ("player_leave", guest_id))
        resume_reader, resume_writer = await asyncio.open_connection("127.0.0.1", self.port)
        await receive(resume_reader)
        await send(resume_writer, {"cmd": "resume", "player_id": guest_id,
                                   "room": "ADMIN",
                                   "resume_token": guest_joined["resume_token"]})
        self.assertEqual((await receive(resume_reader))["code"], "resume_denied")

        late_id = late_joined["player_id"]
        late_actor = room.players[late_id].actor_id
        await send(owner_writer, {"cmd": "interaction", "kind": "room_kick",
                                  "target_actor": late_actor, "data": ""})
        kicked = await receive(late_reader)
        self.assertEqual((kicked["type"], kicked["code"]), ("error", "room_kicked"))
        left = await receive(owner_reader)
        self.assertEqual((left["type"], left["player_id"]), ("player_leave", late_id))
        self.assertNotIn(late_id, room.players)
        self.assertEqual(owner_actor, room.players[owner_id].actor_id)
        for writer in (owner_writer, late_writer, resume_writer): writer.close()
        for writer in (owner_writer, late_writer, resume_writer):
            try: await writer.wait_closed()
            except (ConnectionError, OSError): pass

    async def test_real_player_shinwa_listing_is_single_claim(self):
        seller_reader, seller_writer, seller_id = await self.connect("Seller")
        buyer_reader, buyer_writer, buyer_id = await self.connect("Buyer")
        await send(seller_writer, {"cmd": "create", "room": "SHINWA"})
        await receive(seller_reader)
        await send(buyer_writer, {"cmd": "join", "room": "SHINWA"})
        await receive(buyer_reader); await receive(seller_reader)
        expires = int(__import__("time").time() * 1000) + 60000
        listing_data = f"0,42,2,4,2,500000,{expires},-1,6:10,7:20"
        await send(seller_writer, {"cmd": "interaction", "kind": "shinwa_publish",
                                   "target_actor": 0, "data": listing_data})
        seller_listing = await receive(seller_reader)
        buyer_listing = await receive(buyer_reader)
        self.assertEqual((seller_listing["type"], buyer_listing["type"]),
                         ("shinwa_listing", "shinwa_listing"))
        product = buyer_listing["item_id"]
        self.assertGreaterEqual(product, 1000000)
        self.assertEqual(buyer_listing["seller_id"], seller_id)
        await send(buyer_writer, {"cmd": "interaction", "kind": "shinwa_buy",
                                  "target_actor": 0, "data": str(product)})
        sold_to_seller = await receive(seller_reader)
        sold_to_buyer = await receive(buyer_reader)
        self.assertEqual((sold_to_seller["type"], sold_to_buyer["buyer_id"]),
                         ("shinwa_sold", buyer_id))
        self.assertNotIn(product, self.state.rooms["SHINWA"].shinwa)
        await send(buyer_writer, {"cmd": "interaction", "kind": "shinwa_buy",
                                  "target_actor": 0, "data": str(product)})
        self.assertEqual((await receive(buyer_reader))["code"], "shinwa_sold")
        seller_writer.close(); buyer_writer.close()
        await seller_writer.wait_closed(); await buyer_writer.wait_closed()

    async def test_owner_authoritative_multiplayer_vxmm_bet_and_payout(self):
        owner_reader, owner_writer, owner_id = await self.connect("Owner")
        guest_reader, guest_writer, guest_id = await self.connect("LuckyGuest")
        await send(owner_writer, {"cmd": "create", "room": "VXMM"})
        await receive(owner_reader)
        await send(guest_writer, {"cmd": "join", "room": "VXMM"})
        await receive(guest_reader); await receive(owner_reader)
        await send(guest_writer, {"cmd": "interaction", "kind": "vxmm_bet",
                                  "target_actor": 0, "data": "0,250000"})
        owner_bet = await receive(owner_reader); guest_bet = await receive(guest_reader)
        self.assertEqual((owner_bet["type"], owner_bet["amount"], guest_bet["name"]),
                         ("vxmm_bet", 250000, "LuckyGuest"))
        self.assertEqual(self.state.rooms["VXMM"].lucky_bets[0][guest_id], 250000)
        await send(guest_writer, {"cmd": "interaction", "kind": "vxmm_result",
                                  "target_actor": 0,
                                  "data": f"0,{guest_id},500000,250000,LuckyGuest"})
        self.assertEqual((await receive(guest_reader))["code"], "owner_world_only")
        await send(owner_writer, {"cmd": "interaction", "kind": "vxmm_result",
                                  "target_actor": 0,
                                  "data": f"0,{guest_id},500000,250000,LuckyGuest"})
        owner_result = await receive(owner_reader); guest_result = await receive(guest_reader)
        self.assertEqual((owner_result["type"], guest_result["target_id"],
                          guest_result["amount"]), ("vxmm_result", guest_id, 500000))
        self.assertEqual(self.state.rooms["VXMM"].lucky_bets[0], {})
        owner_notice = await receive(owner_reader); guest_notice = await receive(guest_reader)
        self.assertEqual((owner_notice["type"], owner_notice["channel"], owner_notice["name"]),
                         ("chat", "world", "Admin"))
        self.assertEqual(owner_notice["text"], guest_notice["text"])
        self.assertIn("LUCKYGUEST", guest_notice["text"])
        owner_writer.close(); guest_writer.close()
        await owner_writer.wait_closed(); await guest_writer.wait_closed()

    async def test_owner_authoritative_chan_le_round(self):
        owner_reader, owner_writer, owner_id = await self.connect("ChanLeOwner")
        guest_reader, guest_writer, guest_id = await self.connect("ChanLeGuest")
        await send(owner_writer, {"cmd": "create", "room": "CHANLE"})
        await receive(owner_reader)
        await send(guest_writer, {"cmd": "join", "room": "CHANLE"})
        await receive(guest_reader); await receive(owner_reader)
        await send(guest_writer, {"cmd": "interaction", "kind": "chan_le_bet",
                                  "target_actor": 0, "data": "2000000,1"})
        owner_bet = await receive(owner_reader); guest_bet = await receive(guest_reader)
        self.assertEqual((owner_bet["type"], guest_bet["amount"], guest_bet["relation_type"]),
                         ("chan_le_bet", 2000000, 1))
        self.assertEqual(self.state.rooms["CHANLE"].chan_le_bets[guest_id]["amount"], 2000000)
        await send(guest_writer, {"cmd": "interaction", "kind": "chan_le_result",
                                  "target_actor": 0, "data": "3,1,123,456,21"})
        self.assertEqual((await receive(guest_reader))["code"], "owner_world_only")
        await send(owner_writer, {"cmd": "interaction", "kind": "chan_le_result",
                                  "target_actor": 0, "data": "3,1,123,456,21"})
        owner_result = await receive(owner_reader); guest_result = await receive(guest_reader)
        self.assertEqual((owner_result["type"], guest_result["relation_type"],
                          guest_result["score"]), ("chan_le_result", 1, 21))
        self.assertEqual(self.state.rooms["CHANLE"].chan_le_bets, {})
        owner_writer.close(); guest_writer.close()
        await owner_writer.wait_closed(); await guest_writer.wait_closed()

    async def test_event_rank_state_merges_real_room_players(self):
        a_reader, a_writer, a_id = await self.connect("TopA")
        b_reader, b_writer, b_id = await self.connect("TopB")
        await send(a_writer, {"cmd": "create", "room": "RANK"}); await receive(a_reader)
        await send(b_writer, {"cmd": "join", "room": "RANK"})
        await receive(b_reader); await receive(a_reader)
        await send(a_writer, {"cmd": "interaction", "kind": "rank_report",
                              "target_actor": 0, "data": "event-3-1,25"})
        first_a = await receive(a_reader); first_b = await receive(b_reader)
        self.assertEqual(first_a["type"], "rank_state")
        self.assertIn(f"{a_id}:TopA:25", first_b["data"])
        await send(b_writer, {"cmd": "interaction", "kind": "rank_report",
                              "target_actor": 0, "data": "event-3-1,40"})
        second_a = await receive(a_reader); second_b = await receive(b_reader)
        self.assertTrue(second_a["data"].index(f"{b_id}:TopB:40")
                        < second_a["data"].index(f"{a_id}:TopA:25"))
        self.assertEqual(second_b["type"], "rank_state")
        a_writer.close(); b_writer.close()
        await a_writer.wait_closed(); await b_writer.wait_closed()

    async def test_dungeon_settlement_is_owner_authoritative_and_broadcast_to_guests(self):
        owner_reader, owner_writer, owner_id = await self.connect("CaveOwner")
        guest_reader, guest_writer, guest_id = await self.connect("CaveGuest")
        await send(owner_writer, {"cmd": "create", "room": "CAVE"}); await receive(owner_reader)
        await send(guest_writer, {"cmd": "join", "room": "CAVE"})
        await receive(guest_reader); await receive(owner_reader)
        await send(guest_writer, {"cmd": "interaction", "kind": "dungeon_finish",
                                  "target_actor": 0, "data": "2,850,4"})
        self.assertEqual((await receive(guest_reader))["code"], "owner_world_only")
        await send(owner_writer, {"cmd": "interaction", "kind": "dungeon_finish",
                                  "target_actor": 0, "data": "2,850,4"})
        settled = await receive(guest_reader)
        self.assertEqual((settled["type"], settled["category"], settled["amount"],
                          settled["quantity"], settled["owner_id"]),
                         ("dungeon_finish", 2, 850, 4, owner_id))
        self.assertEqual(guest_id in self.state.rooms["CAVE"].players, True)
        owner_writer.close(); guest_writer.close()
        await owner_writer.wait_closed(); await guest_writer.wait_closed()

    async def test_room_capacity_is_twelve_including_owner(self):
        owner_reader, owner_writer, _ = await self.connect("Owner")
        await send(owner_writer, {"cmd": "create", "room": "TWELVE",
                                  "cheat_enabled": False})
        await receive(owner_reader)
        guests = []
        for index in range(11):
            reader, writer, _ = await self.connect("G" + str(index))
            await send(writer, {"cmd": "join", "room": "TWELVE",
                                "cheat_enabled": False})
            self.assertEqual((await receive(reader))["type"], "room_joined")
            guests.append((reader, writer))
        self.assertEqual(len(self.state.rooms["TWELVE"].players), 12)
        overflow_reader, overflow_writer, _ = await self.connect("Overflow")
        await send(overflow_writer, {"cmd": "join", "room": "TWELVE",
                                     "cheat_enabled": False})
        full = await receive(overflow_reader)
        self.assertEqual((full["type"], full["code"]), ("error", "room_full"))
        self.assertEqual(len(self.state.rooms["TWELVE"].players), 12)
        owner_writer.close(); overflow_writer.close()
        for _, writer in guests: writer.close()
        await owner_writer.wait_closed(); await overflow_writer.wait_closed()
        for _, writer in guests: await writer.wait_closed()

    async def test_twelve_client_binary_state_soak_remains_bounded(self):
        self.state.log = lambda *args, **kwargs: None
        clients = [await self.connect("Soak" + str(index)) for index in range(12)]
        owner_reader, owner_writer, _ = clients[0]
        await send(owner_writer, {"cmd": "create", "room": "SOAK12"})
        await receive(owner_reader)
        joined = [clients[0]]
        for client in clients[1:]:
            reader, writer, _ = client
            await send(writer, {"cmd": "join", "room": "SOAK12"})
            await receive(reader)
            for existing_reader, _, _ in joined:
                self.assertEqual((await receive(existing_reader))["type"], "player_join")
            joined.append(client)

        counts = [{} for _ in clients]

        async def drain(index, reader):
            try:
                while True:
                    event = await receive(reader)
                    event_type = event["type"]
                    counts[index][event_type] = counts[index].get(event_type, 0) + 1
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                return

        drains = [asyncio.create_task(drain(index, client[0]))
                  for index, client in enumerate(clients)]

        async def publish(index, writer):
            for step in range(25):
                await send(writer, {"cmd": "state", "map": 1,
                                    "x": 40 + index * 4 + step,
                                    "y": 80 + (step % 3), "hp": 100})

        await asyncio.gather(*(publish(index, client[1])
                               for index, client in enumerate(clients)))
        await asyncio.sleep(0.75)
        room = self.state.rooms["SOAK12"]
        self.assertEqual(len(room.players), 12)
        self.assertGreaterEqual(room.sequence, 300)
        observed = sum(row.get("player_state", 0) for row in counts)
        self.assertGreaterEqual(observed, 3000)
        self.assertTrue(all(not client[1].is_closing() for client in clients))

        for _, writer, _ in clients: writer.close()
        for _, writer, _ in clients: await writer.wait_closed()
        await asyncio.gather(*drains, return_exceptions=True)

    async def test_native_party_trade_and_duel_relay(self):
        a_reader, a_writer, a_id = await self.connect("A")
        b_reader, b_writer, b_id = await self.connect("B")
        await send(a_writer, '{"cmd":"create","room":"SOCIAL"}')
        await a_writer.drain(); await receive(a_reader)
        await send(b_writer, '{"cmd":"join","room":"SOCIAL"}')
        await b_writer.drain(); await receive(b_reader); await receive(a_reader)
        a_actor = self.state.players[a_id].actor_id
        b_actor = self.state.players[b_id].actor_id
        self.state.players[a_id].appearance = {"damage": 60}
        self.state.players[b_id].appearance = {"damage_down": 0, "max_hp": 50}
        await send(a_writer, '{"cmd":"state","map":22,"x":100,"y":200,"hp":100}')
        await a_writer.drain(); await receive(b_reader)
        await send(b_writer, '{"cmd":"state","map":22,"x":120,"y":200,"hp":50}')
        await b_writer.drain(); await receive(a_reader)

        await send(a_writer, {"cmd": "interaction", "kind": "party_invite",
                                    "target_actor": b_actor})
        await a_writer.drain()
        self.assertEqual((await receive(b_reader))["type"], "party_invite")
        await send(b_writer, {"cmd": "interaction", "kind": "party_accept",
                                    "target_actor": a_actor})
        await b_writer.drain()
        a_party = await receive(a_reader)
        self.assertEqual(a_party["type"], "party_roster", a_party)
        self.assertEqual((await receive(b_reader))["type"], "party_roster")
        await send(a_writer, {"cmd": "interaction", "kind": "party_chat",
                                    "data": "chat nhom"})
        await a_writer.drain()
        self.assertEqual((await receive(a_reader))["text"], "chat nhom")
        self.assertEqual((await receive(b_reader))["text"], "chat nhom")

        await send(a_writer, {"cmd": "interaction", "kind": "trade_invite",
                                    "target_actor": b_actor})
        await a_writer.drain()
        self.assertEqual((await receive(b_reader))["type"], "trade_invite")
        await send(b_writer, {"cmd": "interaction", "kind": "trade_accept",
                                    "target_actor": a_actor})
        await b_writer.drain()
        self.assertEqual((await receive(b_reader))["type"], "trade_open")
        self.assertEqual((await receive(a_reader))["type"], "trade_open")
        offer_a = "100|0,17,2,0,0,-1,0"
        offer_b = "200|1,18,3,0,0,-1,0"
        await send(a_writer, {"cmd": "interaction", "kind": "trade_offer",
                                    "target_actor": b_actor, "data": offer_a})
        await a_writer.drain()
        self.assertEqual((await receive(b_reader))["data"], offer_a)
        await send(b_writer, {"cmd": "interaction", "kind": "trade_offer",
                                    "target_actor": a_actor, "data": offer_b})
        await b_writer.drain()
        self.assertEqual((await receive(a_reader))["data"], offer_b)
        await send(a_writer, {"cmd": "interaction", "kind": "trade_confirm",
                                    "target_actor": b_actor})
        await a_writer.drain()
        self.assertEqual((await receive(b_reader))["type"], "trade_confirmed")
        await send(b_writer, {"cmd": "interaction", "kind": "trade_confirm",
                                    "target_actor": a_actor})
        await b_writer.drain()
        self.assertEqual((await receive(a_reader))["type"], "trade_confirmed")
        b_prepare = await receive(b_reader); a_prepare = await receive(a_reader)
        self.assertEqual((b_prepare["type"], b_prepare["peer_offer"]),
                         ("trade_prepare", offer_a))
        self.assertEqual((a_prepare["type"], a_prepare["peer_offer"]),
                         ("trade_prepare", offer_b))
        self.assertEqual(a_prepare["transaction"], b_prepare["transaction"])
        await send(a_writer, {"cmd": "interaction", "kind": "trade_ready",
                              "target_actor": b_actor, "data": a_prepare["transaction"]})
        await send(b_writer, {"cmd": "interaction", "kind": "trade_ready",
                              "target_actor": a_actor, "data": b_prepare["transaction"]})
        b_commit = await receive(b_reader); a_commit = await receive(a_reader)
        self.assertEqual((b_commit["type"], b_commit["peer_offer"]), ("trade_commit", offer_a))
        self.assertEqual((a_commit["type"], a_commit["peer_offer"]), ("trade_commit", offer_b))
        self.assertEqual(a_commit["transaction"], b_commit["transaction"])

        await send(a_writer, {"cmd": "interaction", "kind": "duel_invite",
                                    "target_actor": b_actor})
        await a_writer.drain()
        self.assertEqual((await receive(b_reader))["type"], "duel_invite")
        await send(b_writer, {"cmd": "interaction", "kind": "duel_accept",
                                    "target_actor": a_actor})
        await b_writer.drain()
        self.assertEqual((await receive(b_reader))["type"], "duel_start")
        self.assertEqual((await receive(a_reader))["type"], "duel_start")
        await send(a_writer, {"cmd": "interaction", "kind": "duel_attack",
                                    "target_actor": b_actor, "damage": 60, "skill": 7})
        await a_writer.drain()
        self.assertEqual((await receive(a_reader))["type"], "duel_hit")
        self.assertEqual((await receive(b_reader))["type"], "duel_hit")
        self.assertEqual((await receive(a_reader))["type"], "duel_end")
        self.assertEqual((await receive(b_reader))["type"], "duel_end")
        self.assertNotIn(a_id, self.state.rooms["SOCIAL"].duels)
        a_writer.close(); b_writer.close()
        await a_writer.wait_closed(); await b_writer.wait_closed()

    async def test_party_exp_ten_percent_reaches_only_eligible_same_map_members(self):
        a_reader, a_writer, _ = await self.connect("Leader")
        b_reader, b_writer, _ = await self.connect("Near")
        c_reader, c_writer, _ = await self.connect("Far")
        await send(a_writer, {"cmd": "create", "room": "PARTYEXP"}); await receive(a_reader)
        await send(b_writer, {"cmd": "join", "room": "PARTYEXP"}); await receive(b_reader); await receive(a_reader)
        await send(c_writer, {"cmd": "join", "room": "PARTYEXP"}); await receive(c_reader); await receive(a_reader); await receive(b_reader)
        await send(a_writer, {"cmd": "appearance", "name": "Leader", "level": 60});
        await receive(b_reader); await receive(c_reader)
        await send(b_writer, {"cmd": "appearance", "name": "Near", "level": 65});
        await receive(a_reader); await receive(c_reader)
        await send(c_writer, {"cmd": "appearance", "name": "Far", "level": 80});
        await receive(a_reader); await receive(b_reader)
        a_actor = self.state.players[next(pid for pid, p in self.state.players.items()
                                          if p.name == "Leader")].actor_id
        b_actor = self.state.players[next(pid for pid, p in self.state.players.items()
                                          if p.name == "Near")].actor_id
        await send(a_writer, {"cmd": "state", "map": 22, "x": 10, "y": 10, "hp": 100})
        await receive(b_reader); await receive(c_reader)
        await send(b_writer, {"cmd": "state", "map": 22, "x": 20, "y": 10, "hp": 100})
        await receive(a_reader); await receive(c_reader)
        await send(c_writer, {"cmd": "state", "map": 22, "x": 30, "y": 10, "hp": 100})
        await receive(a_reader); await receive(b_reader)
        await send(a_writer, {"cmd": "interaction", "kind": "party_invite",
                              "target_actor": b_actor}); await receive(b_reader)
        await send(b_writer, {"cmd": "interaction", "kind": "party_accept",
                              "target_actor": a_actor}); await receive(a_reader); await receive(b_reader)
        # Add the third player to the same party, but its 20-level difference
        # must exclude it from the NSO_FINAL group EXP branch.
        c_actor = next(p.actor_id for p in self.state.rooms["PARTYEXP"].players.values()
                       if p.name == "Far")
        await send(a_writer, {"cmd": "interaction", "kind": "party_invite",
                              "target_actor": c_actor}); await receive(c_reader)
        await send(c_writer, {"cmd": "interaction", "kind": "party_accept",
                              "target_actor": a_actor})
        await receive(a_reader); await receive(b_reader); await receive(c_reader)
        await send(a_writer, {"cmd": "attack", "mob_id": "9", "damage": 10,
                              "hp": 90, "max_hp": 100, "party_exp": 1234})
        self.assertEqual((await receive(b_reader))["type"], "mob_state")
        reward = await receive(b_reader)
        self.assertEqual((reward["type"], reward["amount"]), ("party_reward", 1234))
        self.assertEqual((await receive(c_reader))["type"], "mob_state")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(receive(c_reader), 0.05)
        for writer in (a_writer, b_writer, c_writer): writer.close()
        for writer in (a_writer, b_writer, c_writer): await writer.wait_closed()

    async def test_party_leader_lock_kick_and_dungeon_admission(self):
        a_reader, a_writer, a_id = await self.connect("Leader")
        b_reader, b_writer, b_id = await self.connect("Member")
        c_reader, c_writer, c_id = await self.connect("Third")
        await send(a_writer, {"cmd": "create", "room": "PARTYRULES"}); await receive(a_reader)
        await send(b_writer, {"cmd": "join", "room": "PARTYRULES"}); await receive(b_reader); await receive(a_reader)
        await send(c_writer, {"cmd": "join", "room": "PARTYRULES"}); await receive(c_reader); await receive(a_reader); await receive(b_reader)
        room = self.state.rooms["PARTYRULES"]
        actors = {pid: room.players[pid].actor_id for pid in (a_id, b_id, c_id)}
        for writer, reader, pid, name, level, others in (
                (a_writer, a_reader, a_id, "Leader", 75, (b_reader, c_reader)),
                (b_writer, b_reader, b_id, "Member", 76, (a_reader, c_reader)),
                (c_writer, c_reader, c_id, "Third", 77, (a_reader, b_reader))):
            await send(writer, {"cmd": "appearance", "name": name, "level": level,
                                "max_hp": 100, "damage": 20})
            for other in others: self.assertEqual((await receive(other))["type"], "player_appearance")

        await send(a_writer, {"cmd": "interaction", "kind": "party_invite",
                              "target_actor": actors[b_id]}); await receive(b_reader)
        await send(b_writer, {"cmd": "interaction", "kind": "party_accept",
                              "target_actor": actors[a_id]})
        await receive(a_reader); await receive(b_reader)
        room.players[b_id].hp = 100
        await send(a_writer, {"cmd": "attack", "mob_id": "9", "damage": 1,
                              "hp": 10, "max_hp": 10, "drop_template": 77,
                              "level_boss": 2})
        for reader in (a_reader, b_reader, c_reader):
            self.assertEqual((await receive(reader))["type"], "mob_state")
        await send(a_writer, {"cmd": "attack", "mob_id": "9", "damage": 10,
                              "hp": 0, "max_hp": 10, "drop_template": 77,
                              "level_boss": 2})
        self.assertEqual((await receive(a_reader))["type"], "mob_state")
        self.assertEqual((await receive(b_reader))["type"], "mob_state")
        shared_kill = await receive(b_reader)
        self.assertEqual((shared_kill["type"], shared_kill["template"],
                          shared_kill["level_boss"]), ("party_kill", 77, 2))
        self.assertEqual((await receive(c_reader))["type"], "mob_state")
        await send(b_writer, {"cmd": "interaction", "kind": "party_invite",
                              "target_actor": actors[c_id]})
        self.assertEqual((await receive(b_reader))["code"], "party_leader_only")
        await send(a_writer, {"cmd": "interaction", "kind": "party_invite",
                              "target_actor": actors[c_id]}); await receive(c_reader)
        await send(c_writer, {"cmd": "interaction", "kind": "party_accept",
                              "target_actor": actors[a_id]})
        for reader in (a_reader, b_reader, c_reader):
            self.assertEqual((await receive(reader))["type"], "party_roster")

        await send(a_writer, {"cmd": "interaction", "kind": "party_lock",
                              "target_actor": 0, "data": "true"})
        for reader in (a_reader, b_reader, c_reader):
            roster = await receive(reader); self.assertTrue(roster["locked"])

        await send(b_writer, {"cmd": "interaction", "kind": "party_leader",
                              "target_actor": actors[c_id]})
        self.assertEqual((await receive(b_reader))["code"], "party_leader_only")
        await send(a_writer, {"cmd": "interaction", "kind": "party_leader",
                              "target_actor": actors[b_id]})
        for reader in (a_reader, b_reader, c_reader):
            roster = await receive(reader)
            self.assertEqual(roster["leader_id"], b_id)
            self.assertTrue(roster["locked"])

        await send(b_writer, {"cmd": "interaction", "kind": "party_kick",
                              "target_actor": actors[c_id]})
        self.assertEqual((await receive(c_reader))["type"], "party_closed")
        for reader in (a_reader, b_reader):
            roster = await receive(reader); self.assertEqual(len(roster["members"]), 2)

        await send(a_writer, {"cmd": "interaction", "kind": "dungeon_open",
                              "target_actor": 0, "data": "5"})
        self.assertEqual((await receive(a_reader))["code"], "party_leader_only")
        await send(b_writer, {"cmd": "interaction", "kind": "dungeon_open",
                              "target_actor": 0, "data": "5"})
        self.assertEqual((await receive(b_reader))["code"], "owner_world_only")

        await send(b_writer, {"cmd": "interaction", "kind": "party_leader",
                              "target_actor": actors[a_id]})
        for reader in (a_reader, b_reader):
            self.assertEqual((await receive(reader))["leader_id"], a_id)
        await send(a_writer, {"cmd": "interaction", "kind": "dungeon_open",
                              "target_actor": 0, "data": "4"})
        self.assertEqual((await receive(a_reader))["code"], "dungeon_solo_only")
        await send(a_writer, {"cmd": "interaction", "kind": "dungeon_open",
                              "target_actor": 0, "data": "5"})
        for reader in (a_reader, b_reader):
            admitted = await receive(reader)
            self.assertEqual((admitted["type"], admitted["category"], admitted["leader_id"]),
                             ("dungeon_admit", 5, a_id))
        self.assertEqual(room.party_dungeons[a_id], 5)
        for writer in (a_writer, b_writer, c_writer): writer.close()
        for writer in (a_writer, b_writer, c_writer): await writer.wait_closed()

    async def test_owner_world_packet_relay_and_validation(self):
        owner_reader, owner_writer, owner_id = await self.connect("WorldOwner")
        guest_reader, guest_writer, guest_id = await self.connect("WorldGuest")
        await send(owner_writer, {"cmd": "create", "room": "WORLDRELAY"})
        await receive(owner_reader)
        await send(guest_writer, {"cmd": "join", "room": "WORLDRELAY"})
        await receive(guest_reader); await receive(owner_reader)

        await send(guest_writer, {"cmd": "interaction", "kind": "world_packet",
                                  "target_actor": 0, "damage": 122,
                                  "data": "0102"})
        self.assertEqual((await receive(guest_reader))["code"], "owner_world_only")

        await send(owner_writer, {"cmd": "interaction", "kind": "world_packet",
                                  "target_actor": 0, "damage": 122,
                                  "data": "not-hex"})
        self.assertEqual((await receive(owner_reader))["code"], "world_packet_invalid")

        await send(owner_writer, {"cmd": "interaction", "kind": "world_packet",
                                  "target_actor": 0, "damage": -30,
                                  "data": "a10000003c"})
        relayed = await receive(guest_reader)
        self.assertEqual((relayed["type"], relayed["player_id"],
                          relayed["command"], relayed["data"]),
                         ("world_packet", owner_id, -30, "a10000003c"))
        self.assertNotEqual(relayed["player_id"], guest_id)
        owner_writer.close(); guest_writer.close()
        await owner_writer.wait_closed(); await guest_writer.wait_closed()

    async def test_owner_world_yen_reward_is_durable_for_remote_killer(self):
        owner_reader, owner_writer, _ = await self.connect("RewardOwner")
        guest_reader, guest_writer, guest_id = await self.connect("RewardGuest")
        await send(owner_writer, {"cmd": "create", "room": "WORLDREWARD"})
        await receive(owner_reader)
        await send(guest_writer, {"cmd": "join", "room": "WORLDREWARD"})
        await receive(guest_reader); await receive(owner_reader)
        room = self.state.rooms["WORLDREWARD"]
        guest_actor = room.players[guest_id].actor_id

        await send(guest_writer, {"cmd": "interaction", "kind": "mob_yen",
                                  "target_actor": room.players[room.owner_id].actor_id,
                                  "damage": 123})
        self.assertEqual((await receive(guest_reader))["code"], "owner_world_only")

        await send(owner_writer, {"cmd": "interaction", "kind": "mob_yen",
                                  "target_actor": guest_actor, "damage": 4567})
        reward = await receive(guest_reader)
        self.assertEqual((reward["type"], reward["amount"]), ("mob_reward", 4567))
        delivery = reward["delivery_id"]
        self.assertIn(delivery, room.pending_deliveries[guest_id])
        await send(guest_writer, {"cmd": "interaction", "kind": "delivery_ack",
                                  "target_actor": 0, "data": delivery})
        await asyncio.sleep(0.05)
        self.assertNotIn(guest_id, room.pending_deliveries)
        owner_writer.close(); guest_writer.close()
        await owner_writer.wait_closed(); await guest_writer.wait_closed()

    async def test_owner_mob_ai_hits_remote_player_authoritatively(self):
        owner_reader, owner_writer, _ = await self.connect("AiOwner")
        guest_reader, guest_writer, guest_id = await self.connect("AiGuest")
        await send(owner_writer, {"cmd": "create", "room": "MOBAI"}); await receive(owner_reader)
        await send(guest_writer, {"cmd": "join", "room": "MOBAI"})
        await receive(guest_reader); await receive(owner_reader)
        room = self.state.rooms["MOBAI"]
        guest_actor = room.players[guest_id].actor_id
        await send(guest_writer, {"cmd": "appearance", "name": "AiGuest",
                                  "damage_down": 10, "res_fire": 20})
        await receive(owner_reader)
        await send(guest_writer, {"cmd": "state", "map": 1, "x": 110, "y": 100, "hp": 100})
        await receive(owner_reader)

        await send(guest_writer, {"cmd": "interaction", "kind": "mob_attack",
                                  "target_actor": room.players[room.owner_id].actor_id,
                                  "data": "1,100,100", "damage": 100, "skill": 1})
        self.assertEqual((await receive(guest_reader))["code"], "owner_world_only")
        await send(owner_writer, {"cmd": "interaction", "kind": "mob_attack",
                                  "target_actor": guest_actor, "data": "1,100,100",
                                  "damage": 100, "skill": 1})
        owner_hit = await receive(owner_reader); guest_hit = await receive(guest_reader)
        for hit in (owner_hit, guest_hit):
            self.assertEqual((hit["type"], hit["damage"], hit["amount"], hit["hp"]),
                             ("mob_hit", 70, 14, 86))
        self.assertEqual(room.players[guest_id].hp, 86)

        await send(owner_writer, {"cmd": "interaction", "kind": "mob_attack",
                                  "target_actor": guest_actor, "data": "1,999,999",
                                  "damage": 100, "skill": 1})
        self.assertEqual((await receive(owner_reader))["code"], "mob_attack_range")
        owner_writer.close(); guest_writer.close()
        await owner_writer.wait_closed(); await guest_writer.wait_closed()

    async def test_restart_discards_stale_room_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "rooms.json")
            Path(path).write_text(json.dumps({"version": 4,
                "rooms": {"PERSIST": [{"player_id": "p1", "name": "A"}]},
                "worlds": {"PERSIST": {"owner_id": "p1"}}}), encoding="utf-8")
            loaded = RoomServer(path)
            self.assertNotIn("PERSIST", loaded.saved_rooms)
            self.assertNotIn("PERSIST", loaded.saved_worlds)

    async def test_same_mob_id_is_isolated_by_map(self):
        reader, writer, player_id = await self.connect("A")
        await send(writer, '{"cmd":"create","room":"MAPS"}')
        await writer.drain(); await receive(reader)
        player = self.state.players[player_id]
        room = self.state.rooms["MAPS"]
        player.map_id = 2
        await self.state.command(player, {"cmd": "attack", "mob_id": "7",
                                          "damage": 20, "hp": 80, "max_hp": 100})
        self.assertEqual(room.mobs["2:0:7"]["hp"], 80)
        player.map_id = 3
        await self.state.command(player, {"cmd": "attack", "mob_id": "7",
                                          "damage": 50, "hp": 50, "max_hp": 100})
        self.assertEqual(room.mobs["2:0:7"]["hp"], 80)
        self.assertEqual(room.mobs["3:0:7"]["hp"], 50)
        # Legacy clients may omit authoritative HP; death must use the
        # computed server HP rather than the default reported value.
        await self.state.command(player, {"cmd": "attack", "mob_id": "7",
                                          "damage": 50, "max_hp": 100})
        self.assertEqual(room.mobs["3:0:7"]["hp"], 0)
        self.assertFalse(room.mobs["3:0:7"]["alive"])
        writer.close(); await writer.wait_closed()

    async def test_dead_mob_survives_map_return_until_room_respawn_deadline(self):
        owner_reader, owner_writer, owner_id = await self.connect("Owner")
        guest_reader, guest_writer, guest_id = await self.connect("Guest")
        await send(owner_writer, {"cmd": "create", "room": "MOBSTATE"})
        await receive(owner_reader)
        await send(guest_writer, {"cmd": "join", "room": "MOBSTATE"})
        await receive(guest_reader); await receive(owner_reader)
        room = self.state.rooms["MOBSTATE"]
        room.players[owner_id].map_id = 7; room.players[owner_id].zone_id = 0
        room.players[guest_id].map_id = 7; room.players[guest_id].zone_id = 0

        await send(guest_writer, {"cmd": "attack", "mob_id": "3",
                                  "damage": 100, "hp": 0, "max_hp": 100,
                                  "respawn_seconds": 2})
        owner_death = await receive(owner_reader)
        guest_death = await receive(guest_reader)
        self.assertFalse(owner_death["mob"]["alive"])
        self.assertFalse(guest_death["mob"]["alive"])
        self.assertGreater(room.mobs["7:0:3"]["respawn_at"], int(time.time() * 1000))

        await send(guest_writer, {"cmd": "map", "map": 8, "zone": 0,
                                  "x": 10, "y": 20})
        await receive(owner_reader)
        await send(guest_writer, {"cmd": "map", "map": 7, "zone": 0,
                                  "x": 10, "y": 20})
        await receive(owner_reader)
        await send(guest_writer, {"cmd": "snapshot"})
        snapshot = await receive(guest_reader)
        dead = next(mob for mob in snapshot["mobs"]
                    if mob["map"] == 7 and mob["zone"] == 0 and mob["mob_id"] == "3")
        self.assertFalse(dead["alive"])
        self.assertEqual(dead["hp"], 0)

        owner_respawn = await asyncio.wait_for(receive(owner_reader), 2.5)
        guest_respawn = await asyncio.wait_for(receive(guest_reader), 2.5)
        for event in (owner_respawn, guest_respawn):
            self.assertEqual(event["type"], "mob_state")
            self.assertTrue(event["mob"]["alive"])
            self.assertEqual(event["mob"]["hp"], 100)
            self.assertEqual(event["attacker_actor_id"], 0)
        self.assertEqual(room.mobs["7:0:3"]["respawn_at"], 0)
        owner_writer.close(); guest_writer.close()
        await owner_writer.wait_closed(); await guest_writer.wait_closed()

    async def test_resume_rehydrates_saved_player(self):
        owner_reader, owner_writer, _ = await self.connect("Owner")
        await send(owner_writer, {"cmd": "create", "room": "RESUME"})
        await receive(owner_reader)
        guest_reader, guest_writer, guest_id = await self.connect("Guest")
        await send(guest_writer, {"cmd": "join", "room": "RESUME"})
        joined = await receive(guest_reader); token = joined["resume_token"]
        await receive(owner_reader)
        await send(guest_writer, {"cmd": "state", "map": 6, "x": 21, "y": 22, "hp": 77})
        await receive(owner_reader)
        guest_writer.close(); await guest_writer.wait_closed()
        for _ in range(30):
            if guest_id not in self.state.players: break
            await asyncio.sleep(0.01)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        await receive(reader)
        await send(writer, {"cmd": "resume", "player_id": guest_id,
                            "room": "RESUME", "resume_token": token})
        await writer.drain()
        joined = await receive(reader)
        self.assertEqual(joined["type"], "room_joined")
        self.assertTrue(joined["resume_token"])
        self.assertNotEqual(joined["resume_token"], token)
        restored = [p for p in joined["players"] if p["player_id"] == guest_id][0]
        self.assertEqual((restored["map"], restored["x"], restored["y"], restored["hp"]), (6, 21, 22, 77))
        writer.close(); owner_writer.close()
        await writer.wait_closed(); await owner_writer.wait_closed()

    async def test_owner_disconnect_deletes_room_and_code_is_reusable(self):
        owner_reader, owner_writer, owner_id = await self.connect("Owner")
        await send(owner_writer, {"cmd": "create", "room": "AABBCC"})
        await receive(owner_reader)
        guest_reader, guest_writer, _ = await self.connect("Guest")
        await send(guest_writer, {"cmd": "join", "room": "AABBCC"})
        await receive(guest_reader); await receive(owner_reader)
        owner_writer.close(); await owner_writer.wait_closed()
        closed = await receive(guest_reader)
        self.assertEqual((closed["type"], closed["code"]),
                         ("error", "room_owner_left"))
        for _ in range(30):
            if "AABBCC" not in self.state.rooms: break
            await asyncio.sleep(0.01)
        self.assertNotIn(owner_id, self.state.players)
        self.assertNotIn("AABBCC", self.state.rooms)
        self.assertNotIn("AABBCC", self.state.saved_rooms)
        self.assertNotIn("AABBCC", self.state.saved_worlds)
        next_reader, next_writer, _ = await self.connect("NewOwner")
        await send(next_writer, {"cmd": "create", "room": "AABBCC"})
        self.assertEqual((await receive(next_reader))["type"], "room_joined")
        next_writer.close(); guest_writer.close()
        await next_writer.wait_closed(); await guest_writer.wait_closed()

    async def test_restart_never_rehydrates_room_without_live_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "rooms.json")
            Path(state_path).write_text(json.dumps({"version": 4,
                "rooms": {"RESTART": [{"player_id": "dead-owner", "hp": 75}]},
                "worlds": {"RESTART": {"owner_id": "dead-owner", "sequence": 9}}}),
                encoding="utf-8")
            loaded_state = RoomServer(state_path)
            self.assertEqual(loaded_state.saved_rooms, {})
            self.assertEqual(loaded_state.saved_worlds, {})
            second_server = await asyncio.start_server(loaded_state.handle, "127.0.0.1", 0)
            second_port = second_server.sockets[0].getsockname()[1]
            next_reader, next_writer = await asyncio.open_connection("127.0.0.1", second_port)
            await receive(next_reader)
            await send(next_writer, {"cmd": "create", "room": "RESTART"})
            self.assertEqual((await receive(next_reader))["type"], "room_joined")
            next_writer.close(); await next_writer.wait_closed()
            second_server.close(); await second_server.wait_closed()

    async def test_durable_delivery_replays_while_owner_remains_live(self):
        owner_reader, owner_writer, _ = await self.connect("Owner")
        await send(owner_writer, {"cmd": "create", "room": "LEDGER"})
        await receive(owner_reader)
        reader, writer, player_id = await self.connect("Guest")
        await send(writer, {"cmd": "join", "room": "LEDGER"})
        created = await receive(reader); token = created["resume_token"]
        await receive(owner_reader)
        self.state.rooms["LEDGER"].pending_deliveries[player_id] = {
            "delivery-1": {"type": "trade_commit", "delivery_id": "delivery-1",
                           "transaction": "tx-1", "peer_offer": "0|"}}
        writer.close(); await writer.wait_closed()
        for _ in range(20):
            if player_id not in self.state.players: break
            await asyncio.sleep(0.01)
        next_reader, next_writer = await asyncio.open_connection("127.0.0.1", self.port)
        await receive(next_reader)
        await send(next_writer, {"cmd": "resume", "player_id": player_id,
                                 "room": "LEDGER", "resume_token": token})
        joined = await receive(next_reader)
        self.assertEqual(joined["type"], "room_joined")
        replay = await receive(next_reader)
        self.assertEqual((replay["type"], replay["delivery_id"], replay["transaction"]),
                         ("trade_commit", "delivery-1", "tx-1"))
        await send(next_writer, {"cmd": "interaction", "kind": "delivery_ack",
                                 "target_actor": 0, "data": "delivery-1"})
        await asyncio.sleep(0.05)
        self.assertNotIn(player_id, self.state.rooms["LEDGER"].pending_deliveries)
        next_writer.close(); owner_writer.close()
        await next_writer.wait_closed(); await owner_writer.wait_closed()

    async def test_resume_rejects_missing_wrong_stale_and_active_token(self):
        owner_reader, owner_writer, _ = await self.connect("Owner")
        await send(owner_writer, {"cmd": "create", "room": "TOKEN", "password": "secret"})
        await receive(owner_reader)
        reader, writer, player_id = await self.connect("Guest")
        await send(writer, {"cmd": "join", "room": "TOKEN", "password": "secret"})
        created = await receive(reader); token = created["resume_token"]
        await receive(owner_reader)

        probe_reader, probe_writer = await asyncio.open_connection("127.0.0.1", self.port)
        await receive(probe_reader)
        await send(probe_writer, {"cmd": "resume", "player_id": player_id,
                                  "room": "TOKEN", "resume_token": token})
        self.assertEqual((await receive(probe_reader))["code"], "resume_active")
        probe_writer.close(); await probe_writer.wait_closed()

        writer.close(); await writer.wait_closed()
        for _ in range(20):
            if player_id not in self.state.players: break
            await asyncio.sleep(0.01)

        bad_reader, bad_writer = await asyncio.open_connection("127.0.0.1", self.port)
        await receive(bad_reader)
        await send(bad_writer, {"cmd": "resume", "player_id": player_id,
                                "room": "TOKEN", "resume_token": "wrong"})
        self.assertEqual((await receive(bad_reader))["code"], "resume_denied")
        bad_writer.close(); await bad_writer.wait_closed()

        good_reader, good_writer = await asyncio.open_connection("127.0.0.1", self.port)
        await receive(good_reader)
        await send(good_writer, {"cmd": "resume", "player_id": player_id,
                                 "room": "TOKEN", "resume_token": token})
        resumed = await receive(good_reader)
        self.assertEqual(resumed["type"], "room_joined")
        rotated = resumed["resume_token"]
        self.assertNotEqual(rotated, token)
        good_writer.close(); await good_writer.wait_closed()

        stale_reader, stale_writer = await asyncio.open_connection("127.0.0.1", self.port)
        await receive(stale_reader)
        await send(stale_writer, {"cmd": "resume", "player_id": player_id,
                                  "room": "TOKEN", "resume_token": token})
        self.assertEqual((await receive(stale_reader))["code"], "resume_denied")
        stale_writer.close(); await stale_writer.wait_closed()
        owner_writer.close(); await owner_writer.wait_closed()

    async def test_snapshot_excludes_room_without_live_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "world.json")
            state = RoomServer(state_path)
            room = Room("WORLD")
            room.sequence = 12
            room.mobs["9"] = {"mob_id": "9", "template": 8, "map": 4,
                              "x": 70, "y": 80, "hp": 44, "max_hp": 99, "alive": True}
            room.drops["drop-12"] = {"drop_id": "drop-12", "template": 42,
                                     "map": 4, "x": 71, "y": 81, "owner": "p"}
            state.rooms["WORLD"] = room
            state.save_state()
            await state.flush_state()
            loaded = RoomServer(state_path)
            self.assertNotIn("WORLD", loaded.rooms)
            self.assertNotIn("WORLD", loaded.saved_rooms)
            self.assertNotIn("WORLD", loaded.saved_worlds)

    async def test_exhausted_player_and_class6_revive_are_room_authoritative(self):
        fan_reader, fan_writer, fan_id = await self.connect("Fan")
        target_reader, target_writer, target_id = await self.connect("Target")
        watch_reader, watch_writer, _ = await self.connect("Watcher")
        await send(fan_writer, {"cmd": "create", "room": "REVIVE"}); await receive(fan_reader)
        await send(target_writer, {"cmd": "join", "room": "REVIVE"})
        await receive(target_reader); await receive(fan_reader)
        await send(watch_writer, {"cmd": "join", "room": "REVIVE"})
        await receive(watch_reader); await receive(fan_reader); await receive(target_reader)

        appearances = (
            (fan_writer, (target_reader, watch_reader),
             {"cmd": "appearance", "name": "Fan", "class_id": 6,
              "max_hp": 100, "max_mp": 80, "damage": 200}),
            (target_writer, (fan_reader, watch_reader),
             {"cmd": "appearance", "name": "Target", "class_id": 1,
              "max_hp": 120, "max_mp": 90}),
            (watch_writer, (fan_reader, target_reader),
             {"cmd": "appearance", "name": "Watcher", "class_id": 2,
              "max_hp": 100, "max_mp": 70}),
        )
        for writer, readers, appearance in appearances:
            await send(writer, appearance)
            for reader in readers:
                self.assertEqual((await receive(reader))["type"], "player_appearance")

        states = ((fan_writer, (target_reader, watch_reader), 10, 100),
                  (target_writer, (fan_reader, watch_reader), 20, 40),
                  (watch_writer, (fan_reader, target_reader), 30, 100))
        for writer, readers, x, hp in states:
            await send(writer, {"cmd": "state", "map": 22, "x": x, "y": 10, "hp": hp})
            for reader in readers:
                self.assertEqual((await receive(reader))["type"], "player_state")

        room = self.state.rooms["REVIVE"]
        await send(fan_writer, {"cmd": "interaction", "kind": "cuu_sat",
                                "target_actor": room.players[target_id].actor_id})
        self.assertEqual((await receive(fan_reader))["type"], "cuu_sat_start")
        self.assertEqual((await receive(target_reader))["type"], "cuu_sat_start")
        await send(fan_writer, {"cmd": "interaction", "kind": "pvp_attack",
                                "target_actor": room.players[target_id].actor_id,
                                "skill": 46})
        for reader in (fan_reader, target_reader, watch_reader):
            exhausted = await receive(reader)
            self.assertEqual((exhausted["type"], exhausted["hp"]), ("pvp_hit", 0))
            self.assertEqual((exhausted["attacker"], exhausted["amount"]), ("Fan", 2))
        self.assertEqual((await receive(fan_reader))["type"], "cuu_sat_end")
        self.assertEqual((await receive(target_reader))["type"], "cuu_sat_end")
        self.assertEqual(room.players[target_id].hp, 0)

        await send(fan_writer, {"cmd": "interaction", "kind": "player_revive",
                                "target_actor": room.players[target_id].actor_id,
                                "skill": 49, "data": "200,35"})
        for reader in (fan_reader, target_reader, watch_reader):
            revived = await receive(reader)
            self.assertEqual((revived["type"], revived["skill"], revived["hp"],
                              revived["mp"], revived["template"], revived["damage"]),
                             ("player_revived", 49, 120, 90, 11, 35))
        self.assertEqual(room.players[target_id].hp, 120)

        room.players[target_id].hp = 0
        room.players[fan_id].map_id = room.players[target_id].map_id = 110
        await send(fan_writer, {"cmd": "interaction", "kind": "player_revive",
                                "target_actor": room.players[target_id].actor_id,
                                "skill": 49, "data": "200,35"})
        rejected = await receive(fan_reader)
        self.assertEqual((rejected["type"], rejected["code"]),
                         ("error", "revive_forbidden_map"))
        for writer in (fan_writer, target_writer, watch_writer): writer.close()
        for writer in (fan_writer, target_writer, watch_writer): await writer.wait_closed()

    async def test_owner_activity_reward_is_same_map_durable_and_acknowledged(self):
        owner_reader, owner_writer, owner_id = await self.connect("TrialOwner")
        guest_reader, guest_writer, guest_id = await self.connect("TrialGuest")
        await send(owner_writer, {"cmd": "create", "room": "ACTIVITY"})
        await receive(owner_reader)
        await send(guest_writer, {"cmd": "join", "room": "ACTIVITY"})
        await receive(guest_reader); await receive(owner_reader)
        await send(owner_writer, {"cmd": "state", "map": 180, "x": 100, "y": 200, "hp": 100})
        await receive(guest_reader)
        await send(guest_writer, {"cmd": "state", "map": 180, "x": 120, "y": 200, "hp": 100})
        await receive(owner_reader)
        reward = "Thử Thách Vượt Ải 1|i,1384,2,1;c,3000000;g,500"
        await send(owner_writer, {"cmd": "interaction", "kind": "activity_reward",
                                  "data": reward})
        event = await receive(guest_reader)
        self.assertEqual((event["type"], event["data"], event["activity"]),
                         ("activity_reward", reward, "Thử Thách Vượt Ải 1"))
        delivery = event["delivery_id"]
        self.assertIn(delivery,
                      self.state.rooms["ACTIVITY"].pending_deliveries[guest_id])
        await send(guest_writer, {"cmd": "interaction", "kind": "delivery_ack",
                                  "data": delivery})
        await asyncio.sleep(0.05)
        self.assertNotIn(guest_id,
                         self.state.rooms["ACTIVITY"].pending_deliveries)
        guest_writer.close(); owner_writer.close()
        await guest_writer.wait_closed(); await owner_writer.wait_closed()

    async def test_zone_population_visibility_and_source_drop_ownership(self):
        owner_reader, owner_writer, owner_id = await self.connect("ZoneOwner")
        guest_reader, guest_writer, guest_id = await self.connect("ZoneGuest")
        await send(owner_writer, {"cmd": "create", "room": "ZONES"})
        await receive(owner_reader)
        await send(guest_writer, {"cmd": "join", "room": "ZONES"})
        await receive(guest_reader); await receive(owner_reader)

        await send(owner_writer, {"cmd": "state", "map": 22, "zone": 26,
                                  "x": 100, "y": 200, "hp": 100})
        owner_state = await receive(guest_reader)
        self.assertEqual((owner_state["map"], owner_state["zone"]), (22, 26))
        await send(guest_writer, {"cmd": "state", "map": 22, "zone": 27,
                                  "x": 120, "y": 200, "hp": 100})
        guest_state = await receive(owner_reader)
        self.assertEqual((guest_state["map"], guest_state["zone"]), (22, 27))
        room = self.state.rooms["ZONES"]
        self.assertEqual((room.players[owner_id].zone_id,
                          room.players[guest_id].zone_id), (26, 27))

        await send(guest_writer, {"cmd": "chat", "channel": "map",
                                  "text": "zone 27 only"})
        self.assertEqual((await receive(guest_reader))["text"], "zone 27 only")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(receive(owner_reader), 0.05)

        await send(guest_writer, {"cmd": "map", "map": 22, "zone": 26,
                                  "x": 120, "y": 200})
        transition = await receive(owner_reader)
        self.assertEqual((transition["map"], transition["zone"]), (22, 26))
        await send(guest_writer, {"cmd": "chat", "channel": "map",
                                  "text": "same zone"})
        self.assertEqual((await receive(owner_reader))["text"], "same zone")
        self.assertEqual((await receive(guest_reader))["text"], "same zone")

        await send(owner_writer, {"cmd": "drop", "item_id": 77,
                                  "template": 42, "item_type": 4,
                                  "map": 22, "zone": 26,
                                  "x": 100, "y": 200})
        owner_drop = await receive(owner_reader)
        guest_drop = await receive(guest_reader)
        drop_id = guest_drop["drop"]["drop_id"]
        self.assertEqual((owner_drop["drop"]["zone"],
                          guest_drop["drop"]["owner"]), (26, owner_id))
        await send(guest_writer, {"cmd": "pickup", "drop_id": drop_id})
        denied = await receive(guest_reader)
        self.assertEqual((denied["type"], denied["code"]),
                         ("error", "drop_owned"))
        room.drops[drop_id]["protected_until"] = 0
        await send(guest_writer, {"cmd": "pickup", "drop_id": drop_id})
        self.assertEqual((await receive(owner_reader))["type"], "drop_taken")
        self.assertEqual((await receive(guest_reader))["type"], "drop_taken")

        await send(owner_writer, {"cmd": "drop", "item_id": 78,
                                  "template": 900, "item_type": 25,
                                  "map": 22, "zone": 26,
                                  "x": 100, "y": 200})
        await receive(owner_reader)
        task_drop = await receive(guest_reader)
        task_id = task_drop["drop"]["drop_id"]
        room.drops[task_id]["protected_until"] = 0
        await send(guest_writer, {"cmd": "pickup", "drop_id": task_id})
        task_denied = await receive(guest_reader)
        self.assertEqual((task_denied["type"], task_denied["code"]),
                         ("error", "drop_owned"))
        room.drops.pop(task_id, None)

        # The room owner executes shared-world loot, but the server protects
        # the row for the player whose final hit killed the mob.
        await send(owner_writer, {"cmd": "drop", "item_id": 79,
                                  "template": 545, "item_type": 4,
                                  "owner": guest_id,
                                  "map": 22, "zone": 26,
                                  "x": 100, "y": 200})
        redirected_owner_drop = await receive(owner_reader)
        redirected_guest_drop = await receive(guest_reader)
        redirected_id = redirected_guest_drop["drop"]["drop_id"]
        self.assertEqual(redirected_guest_drop["drop"]["owner"], guest_id)
        await send(owner_writer, {"cmd": "pickup", "drop_id": redirected_id})
        redirected_denied = await receive(owner_reader)
        self.assertEqual((redirected_denied["type"], redirected_denied["code"]),
                         ("error", "drop_owned"))
        await send(guest_writer, {"cmd": "pickup", "drop_id": redirected_id})
        self.assertEqual((await receive(owner_reader))["type"], "drop_taken")
        self.assertEqual((await receive(guest_reader))["type"], "drop_taken")

        # A non-owner cannot assign its loot to another identity.
        await send(guest_writer, {"cmd": "drop", "item_id": 80,
                                  "template": 42, "item_type": 4,
                                  "owner": owner_id,
                                  "map": 22, "zone": 26,
                                  "x": 120, "y": 200})
        forged_owner_view = await receive(owner_reader)
        forged_guest_view = await receive(guest_reader)
        self.assertEqual(forged_guest_view["drop"]["owner"], guest_id)

        # NSO_FINAL removes ordinary ItemMap rows at 30 seconds without
        # waiting for a later pickup, snapshot or another drop command.
        expiring_id = forged_guest_view["drop"]["drop_id"]
        deadline = int(time.time() * 1000) + 20
        room.drops[expiring_id]["expires_at"] = deadline
        await self.state._expire_room_drop(room, expiring_id, deadline)
        expired_owner = await receive(owner_reader)
        expired_guest = await receive(guest_reader)
        for expired in (expired_owner, expired_guest):
            self.assertEqual((expired["type"], expired["drop_id"], expired["player_id"]),
                             ("drop_taken", expiring_id, ""))
        self.assertNotIn(expiring_id, room.drops)
        owner_writer.close(); guest_writer.close()
        await owner_writer.wait_closed(); await guest_writer.wait_closed()

    async def test_owner_publishes_last_hit_loot_for_remote_killer(self):
        owner_reader, owner_writer, owner_id = await self.connect("LootOwner")
        guest_reader, guest_writer, guest_id = await self.connect("LootKiller")
        third_reader, third_writer, third_id = await self.connect("LootLate")
        await send(owner_writer, {"cmd": "create", "room": "LASTHIT"})
        await receive(owner_reader)
        await send(guest_writer, {"cmd": "join", "room": "LASTHIT"})
        await receive(guest_reader); await receive(owner_reader)
        await send(third_writer, {"cmd": "join", "room": "LASTHIT"})
        await receive(third_reader); await receive(owner_reader); await receive(guest_reader)
        room = self.state.rooms["LASTHIT"]
        for player in room.players.values():
            player.map_id = 22; player.zone_id = 4; player.x = 100; player.y = 200

        async def owner_drop(item_id: int):
            await send(owner_writer, {"cmd": "drop", "item_id": item_id,
                                      "template": 42, "item_type": 4,
                                      "map": 22, "zone": 4, "x": 100, "y": 200,
                                      "owner": guest_id})
            events = [await receive(owner_reader), await receive(guest_reader),
                      await receive(third_reader)]
            for event in events:
                self.assertEqual(event["drop"]["owner"], guest_id)
                self.assertNotIn("publisher", event["drop"])
            return events[0]["drop"]["drop_id"]

        protected_id = await owner_drop(91)
        await send(owner_writer, {"cmd": "pickup", "drop_id": protected_id})
        self.assertEqual((await receive(owner_reader))["code"], "drop_owned")
        await send(guest_writer, {"cmd": "pickup", "drop_id": protected_id})
        for reader in (owner_reader, guest_reader, third_reader):
            self.assertEqual((await receive(reader))["player_id"], guest_id)

        unlocked_id = await owner_drop(92)
        room.drops[unlocked_id]["protected_until"] = 0
        await send(third_writer, {"cmd": "pickup", "drop_id": unlocked_id})
        for reader in (owner_reader, guest_reader, third_reader):
            self.assertEqual((await receive(reader))["player_id"], third_id)

        expired_id = await owner_drop(93)
        room.drops[expired_id]["expires_at"] = 1
        await send(owner_writer, {"cmd": "pickup", "drop_id": expired_id})
        self.assertEqual((await receive(owner_reader))["type"], "drop_taken")
        self.assertEqual((await receive(owner_reader))["code"], "drop_expired")
        self.assertEqual((await receive(guest_reader))["type"], "drop_taken")
        self.assertEqual((await receive(third_reader))["type"], "drop_taken")
        self.assertNotIn(expired_id, room.drops)

        for writer in (owner_writer, guest_writer, third_writer): writer.close()
        for writer in (owner_writer, guest_writer, third_writer):
            await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
