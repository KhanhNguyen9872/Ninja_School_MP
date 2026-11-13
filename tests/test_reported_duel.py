"""Real two-client wire regressions for duel damage and map/zone forfeits."""
import asyncio
import copy
import json
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

from test_server import RoomServer, receive, send, _decode_value
from test_eighth_combat import native_profile


class ReportedDuelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = RoomServer(quiet=True)
        self.server = await asyncio.start_server(self.state.handle, "127.0.0.1", 0)
        self.writers = []
        self.events = []
        self.a = await self.connect("A")
        self.b = await self.connect("B")
        self.state.players[self.a[2]].actor_id = 1000000001
        self.state.players[self.b[2]].actor_id = 1000000002
        await send(self.a[1], dict(cmd="create", room="REPORT"))
        await self.event(self.a, "room_joined")
        await send(self.b[1], dict(cmd="join", room="REPORT"))
        await self.event(self.b, "room_joined")
        await self.event(self.a, "player_join")
        for client, peer, name, x in ((self.a, self.b, "A", 100), (self.b, self.a, "B", 120)):
            profile = copy.deepcopy(native_profile())
            profile.update(cmd="appearance", name=name, clan_name="Leaf" if name == "B" else "")
            await send(client[1], profile)
            await self.event(peer, "player_appearance")
            await send(client[1], dict(cmd="state", map=22, zone=0, x=x, y=100, hp=1000))
            await self.event(peer, "player_state")

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.close()
        for writer in self.writers:
            await writer.wait_closed()
        self.server.close()
        await self.server.wait_closed()
        await asyncio.sleep(0)

    async def connect(self, name):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.sockets[0].getsockname()[1])
        self.writers.append(writer)
        welcome = await receive(reader)
        await send(writer, dict(cmd="hello", name=name))
        await receive(reader)
        return reader, writer, welcome["player_id"]

    async def event(self, client, kind):
        for _ in range(40):
            event = await asyncio.wait_for(receive(client[0]), 3)
            self.events.append((client[2], event))
            if event["type"] == kind:
                return event
            self.assertNotEqual(event["type"], "error", event)
        self.fail("missing " + kind)

    def actor(self, client):
        return self.state.players[client[2]].actor_id

    async def start(self):
        await send(self.a[1], dict(cmd="interaction", kind="duel_invite", target_actor=self.actor(self.b)))
        await self.event(self.b, "duel_invite")
        await send(self.b[1], dict(cmd="interaction", kind="duel_accept", target_actor=self.actor(self.a)))
        first = await self.event(self.a, "duel_start")
        self.assertEqual(first, await self.event(self.b, "duel_start"))

    async def test_ordinary_skill_damage_and_visual_ids_reach_both_players(self):
        await self.start()
        for attacker, target in ((self.a, self.b), (self.b, self.a)):
            with patch("nso_server.mixins.combat.secrets.randbelow", return_value=0):
                await send(attacker[1], dict(cmd="interaction", kind="duel_attack",
                    target_actor=self.actor(target), skill=1, damage=2000000000))
                hit = await self.event(target, "duel_hit")
            self.assertEqual(hit, await self.event(attacker, "duel_hit"))
            self.assertGreater(hit["damage"], 0)
            self.assertLess(hit["damage"], 1000)
            self.assertEqual((hit["attacker_actor_id"], hit["target_actor_id"], hit["skill"]),
                             (self.actor(attacker), self.actor(target), 1))
            self.assertEqual(hit["hp"], 1000-hit["damage"])

    async def test_every_map_state_zone_transition_forfeits_once(self):
        for command in ("map", "state"):
            for mover in (self.a, self.b):
                for change_zone in (False, True):
                    with self.subTest(command=command, mover=mover[2], zone=change_zone):
                        # Return both to their common world before a fresh duel.
                        for client in (self.a, self.b):
                            await send(client[1], dict(cmd="map", map=22, zone=0, x=100, y=100))
                            peer = self.b if client is self.a else self.a
                            await self.event(peer, "map_transition")
                        for client in (self.a, self.b):
                            self.state.players[client[2]].command_times.clear()
                        await self.start()
                        winner = self.b if mover is self.a else self.a
                        await send(mover[1], dict(cmd=command, map=22 if change_zone else 27,
                            zone=1 if change_zone else 0, x=110, y=100, hp=1000))
                        ended = await self.event(mover, "duel_end")
                        self.assertEqual(ended, await self.event(winner, "duel_end"))
                        self.assertEqual((ended["loser_id"], ended["winner_id"]), (mover[2], winner[2]))
                        self.assertFalse(self.state.rooms["REPORT"].duels)
                        # A later state/snapshot must not produce a second result.
                        await send(mover[1], dict(cmd="snapshot"))
                        await self.event(mover, "snapshot")
                        self.assertEqual(sum(e["type"] == "duel_end" and e["seq"] == ended["seq"]
                                             for _, e in self.events), 2)

    async def test_same_world_movement_keeps_duel(self):
        await self.start()
        await send(self.a[1], dict(cmd="map", map=22, zone=0, x=101, y=100))
        await self.event(self.b, "map_transition")
        self.assertEqual(len(self.state.rooms["REPORT"].duels), 2)

    async def test_simultaneous_departures_produce_one_shared_result(self):
        await self.start()
        await asyncio.gather(
            send(self.a[1], dict(cmd="map", map=27, zone=0, x=100, y=100)),
            send(self.b[1], dict(cmd="map", map=32, zone=1, x=100, y=100)))
        ended = await self.event(self.a, "duel_end")
        self.assertEqual(ended, await self.event(self.b, "duel_end"))
        self.assertEqual({ended["loser_id"], ended["winner_id"]}, {self.a[2], self.b[2]})
        self.assertFalse(self.state.rooms["REPORT"].duels)
        for client in (self.a, self.b):
            await send(client[1], dict(cmd="snapshot"))
            await self.event(client, "snapshot")
        self.assertEqual(sum(e["type"] == "duel_end" for _, e in self.events), 2)

    async def test_active_duel_links_cannot_be_replaced(self):
        await self.start()
        links = dict(self.state.rooms["REPORT"].duels)
        for kind in ("duel_invite", "duel_accept"):
            await send(self.a[1], dict(cmd="interaction", kind=kind, target_actor=self.actor(self.b)))
            self.assertEqual((await self.event(self.a, "error"))["code"], "duel_busy")
            self.assertEqual(self.state.rooms["REPORT"].duels, links)

    async def test_clan_is_present_in_initial_and_later_snapshots(self):
        for client in (self.a, self.b):
            await send(client[1], dict(cmd="snapshot"))
            snapshot = await self.event(client, "snapshot")
            b = next(row for row in snapshot["players"] if row["player_id"] == self.b[2])
            self.assertEqual(b["appearance"]["clan_name"], "Leaf")

    async def test_live_clan_join_leave_and_kick_reach_peers_and_snapshots(self):
        for action in ("clan_leave", "clan_kick"):
            for client in (self.a, self.b):
                self.state.players[client[2]].command_times.clear()
            await send(self.b[1], dict(cmd="interaction", kind="clan_invite", target_actor=self.actor(self.a), data="Leaf"))
            await self.event(self.a, "clan_invite")
            await send(self.a[1], dict(cmd="interaction", kind="clan_accept", target_actor=self.actor(self.b)))
            self.assertEqual((await self.event(self.a, "clan_joined"))["clan_name"], "Leaf")
            self.assertEqual((await self.event(self.b, "clan_joined"))["clan_name"], "Leaf")
            self.assertEqual(self.state.players[self.a[2]].appearance["clan_name"], "Leaf")
            self.state.rooms["REPORT"].clans["leaf"]["coin"] = 100000
            await send((self.a if action == "clan_leave" else self.b)[1],
                       dict(cmd="interaction", kind=action, target_actor=self.actor(self.a) if action == "clan_kick" else 0))
            for client in (self.a, self.b):
                self.assertEqual((await self.event(client, "clan_joined"))["clan_name"], "")
            await send(self.b[1], dict(cmd="snapshot"))
            snapshot = await self.event(self.b, "snapshot")
            a = next(row for row in snapshot["players"] if row["player_id"] == self.a[2])
            self.assertEqual(a["appearance"]["clan_name"], "")


async def export_native_fixtures(directory, requests):
    """Replay actual Java-produced binary appearance/duel frames over TCP."""
    directory.mkdir(parents=True, exist_ok=True)
    test = ReportedDuelTest("test_ordinary_skill_damage_and_visual_ids_reach_both_players")
    await test.asyncSetUp()
    try:
        await test.start()
        start = next(e for _, e in test.events if e["type"] == "duel_start")
        (directory/"duel-start.json").write_text(json.dumps(start, ensure_ascii=False, separators=(",", ":")))
        data = requests.read_bytes(); offset = 0; hit = None
        while offset < len(data):
            header = data[offset:offset+8]
            size = struct.unpack(">I", header[4:])[0]
            frame = data[offset:offset+8+size]; offset += 8+size
            body, consumed = _decode_value(frame[8:])
            assert header[:3] == b"NS\x02" and consumed == size
            if header[3] == 14:
                test.a[1].write(frame); await test.a[1].drain()
                await test.event(test.b, "player_appearance")
            elif header[3] == 13 and body.get("kind") == "duel_attack":
                with patch("nso_server.mixins.combat.secrets.randbelow", return_value=0):
                    test.a[1].write(frame); await test.a[1].drain()
                    hit = await test.event(test.b, "duel_hit")
                assert hit == await test.event(test.a, "duel_hit")
        assert hit is not None and 0 < hit["damage"] < 1000 and hit["hp"] == 1000-hit["damage"]
        (directory/"duel-hit.json").write_text(json.dumps(hit, ensure_ascii=False, separators=(",", ":")))
        await send(test.a[1], dict(cmd="map", map=27, zone=0, x=100, y=100))
        ended = await test.event(test.a, "duel_end")
        assert ended == await test.event(test.b, "duel_end")
        assert not test.state.rooms["REPORT"].duels
        (directory/"duel-end.json").write_text(json.dumps(ended, ensure_ascii=False, separators=(",", ":")))
        print("ACTUAL_NATIVE_BINARY_MIXED_DUEL_HIT_AND_FORFEIT=PASS")
    finally:
        await test.asyncTearDown()


if __name__ == "__main__":
    if "--fixtures" in sys.argv:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("--fixtures", type=Path, required=True)
        p.add_argument("--requests", type=Path, required=True)
        a = p.parse_args()
        asyncio.run(export_native_fixtures(a.fixtures, a.requests))
    else:
        unittest.main()
