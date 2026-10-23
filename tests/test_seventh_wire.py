"""Audit7 regressions over real loopback NS v2 binary TCP connections.

Server-owned character/clan fixtures are seeded in memory; every tested action
and response crosses the actual protocol encoder, socket, router and decoder.
"""
import asyncio
import time
import unittest

from nso_server.service import RoomServer
from test_server import receive, send


class SeventhWireTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = RoomServer(quiet=True)
        self.listener = await asyncio.start_server(self.state.handle, "127.0.0.1", 0)
        self.port = self.listener.sockets[0].getsockname()[1]
        self.peers = {}
        for name in ("Owner", "Member", "Outside"):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            welcome = await receive(reader)
            await send(writer, {"cmd": "hello", "name": name})
            self.assertEqual((await receive(reader))["type"], "hello_ok")
            peer = {"reader": reader, "writer": writer, "id": welcome["player_id"],
                    "events": [], "queue": asyncio.Queue()}
            self.peers[name] = peer
            peer["pump"] = asyncio.create_task(self.pump(peer))
        await self.command("Owner", {"cmd": "create", "room": "SEVENTH"})
        await self.event("Owner", "room_joined")
        for name in ("Member", "Outside"):
            await self.command(name, {"cmd": "join", "room": "SEVENTH"})
            await self.event(name, "room_joined")
        self.room = self.state.rooms["SEVENTH"]
        for player in self.room.players.values():
            player.appearance.update(level=100, class_id=1)
            player.map_id = 22

    async def pump(self, peer):
        try:
            while True:
                value = await receive(peer["reader"])
                peer["events"].append(value)
                await peer["queue"].put(value)
        except (asyncio.IncompleteReadError, ConnectionError):
            return

    async def asyncTearDown(self):
        for peer in self.peers.values():
            peer["writer"].close()
        for peer in self.peers.values():
            await peer["writer"].wait_closed()
            await peer["pump"]
        self.listener.close()
        await self.listener.wait_closed()
        await asyncio.sleep(0.05)
        self.state._save_executor.shutdown(wait=True)

    async def command(self, who, payload):
        await send(self.peers[who]["writer"], payload)

    async def action(self, who, kind, data="", target=None):
        actor = self.room.players[self.peers[target]["id"]].actor_id if target else 0
        await self.command(who, {"cmd": "interaction", "kind": kind,
                                 "data": data, "target_actor": actor})

    async def event(self, who, kind):
        async def take():
            while True:
                value = await self.peers[who]["queue"].get()
                if value["type"] == kind:
                    return value
        return await asyncio.wait_for(take(), 3)

    async def barrier(self, who):
        await self.command(who, {"cmd": "snapshot"})
        return await self.event(who, "snapshot")

    async def test_dungeon_targeted_durable_finish_no_outsider(self):
        await self.action("Owner", "party_invite", target="Member")
        await self.event("Member", "party_invite")
        await self.action("Member", "party_accept", target="Owner")
        await self.event("Owner", "party_roster")
        await self.action("Owner", "dungeon_open", "6")
        owner = await self.event("Owner", "dungeon_admit")
        member = await self.event("Member", "dungeon_admit")
        self.assertEqual(owner["data"], member["data"])
        await self.command("Member", {"cmd": "map", "map": 157, "x": 100, "y": 100})
        await self.barrier("Member")
        await self.action("Owner", "dungeon_finish", "5,54,3," + owner["data"])
        finish = await self.event("Member", "dungeon_finish")
        await self.barrier("Outside")
        self.assertFalse(any(e["type"] == "dungeon_finish"
                             for e in self.peers["Outside"]["events"]))
        self.assertEqual((finish["data"], finish["quantity"]), (owner["data"], 3))
        pending = self.room.pending_deliveries[self.peers["Member"]["id"]]
        self.assertIn(finish["delivery_id"], pending)
        await self.action("Member", "delivery_ack", finish["delivery_id"])
        await self.barrier("Member")
        self.assertNotIn(self.peers["Member"]["id"], self.room.pending_deliveries)

    async def test_market_expiry_waits_buyer_and_seller_then_safe_reclaim(self):
        expiry = int(time.time() * 1000) + 1500
        raw = f"0,94,1,0,0,1000,{expiry},-1"
        await self.action("Owner", "shinwa_publish", raw)
        product = (await self.event("Owner", "shinwa_listing"))["item_id"]
        await self.action("Member", "shinwa_buy", str(product))
        bought = await self.event("Member", "shinwa_sold")
        await asyncio.sleep(max(0, (expiry - int(time.time() * 1000)) / 1000) + 0.02)
        await self.action("Owner", "shinwa_reclaim", raw)
        self.assertEqual((await self.event("Owner", "error"))["code"], "shinwa_settling")
        await self.action("Member", "delivery_ack", bought["delivery_id"])
        sold = await self.event("Owner", "shinwa_sold")
        self.assertEqual(sold["data"], raw)
        await self.action("Owner", "shinwa_reclaim", raw)
        self.assertEqual((await self.event("Owner", "error"))["code"], "shinwa_settling")
        await self.action("Owner", "delivery_ack", sold["delivery_id"])
        await self.barrier("Owner")
        unsold = f"1,94,1,0,0,1000,{int(time.time() * 1000)-1},-1"
        await self.action("Owner", "shinwa_reclaim", unsold)
        reclaim = await self.event("Owner", "shinwa_removed")
        self.assertEqual((reclaim["category"], reclaim["data"]), (1, unsold))
        await self.action("Owner", "shinwa_reclaim", unsold)
        self.assertEqual((await self.event("Owner", "shinwa_removed"))["delivery_id"],
                         reclaim["delivery_id"])

    async def test_territory_explicit_invite_quota_start_close(self):
        owner_id = self.peers["Owner"]["id"]
        clan = self.state.ensure_clan(self.room, self.room.players[owner_id], "WireClan")
        clan.update(level=10, coin=1000000, exp=500)
        for name in ("Member", "Outside"):
            clan["members"][self.peers[name]["id"]] = {
                "name": name, "role": 0, "class_id": 1, "level": 100, "points": 0}
        await self.action("Owner", "clan_territory_open")
        admission = await self.event("Owner", "clan_territory")
        territory = clan["territory"]
        ident, deadline = territory["id"], territory["deadline"]
        await self.barrier("Member")
        self.assertFalse(any(e["type"] == "clan_territory"
                             for e in self.peers["Member"]["events"]))
        await self.action("Owner", "clan_territory_open")
        retry = await self.event("Owner", "clan_territory")
        self.assertEqual((retry["data"], territory["deadline"], territory["entries"]),
                         (ident, deadline, 0))
        await self.action("Member", "clan_territory_open")
        self.assertEqual((await self.event("Member", "error"))["code"], "clan_territory_denied")
        await self.action("Owner", "clan_territory_invite", target="Member")
        self.assertEqual((await self.event("Member", "clan_territory"))["category"], 3)
        await self.action("Member", "clan_territory_open")
        self.assertEqual((await self.event("Member", "clan_territory"))["data"], admission["data"])
        await self.action("Owner", "clan_territory_sync", f"{ident},1,{deadline}")
        await self.barrier("Owner")
        self.assertTrue(territory["started"])
        await self.action("Outside", "clan_territory_open")
        self.assertEqual((await self.event("Outside", "error"))["code"], "clan_territory_denied")
        await self.action("Owner", "clan_territory_close", f"{ident},1,0")
        await self.barrier("Owner")
        self.assertFalse(territory["active"])
        self.assertFalse(any(e.get("type") == "clan_territory"
                             for p in self.room.pending_deliveries.values() for e in p.values()))
        await self.action("Owner", "clan_territory_open")
        self.assertEqual((await self.event("Owner", "error"))["code"], "clan_territory_no_entries")
