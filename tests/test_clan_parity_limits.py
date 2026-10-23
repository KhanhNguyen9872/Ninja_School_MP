"""Shared-clan atomic quantity admission and explicit self-delivery regressions."""
import unittest
from nso_server.models import Player, Room
from nso_server.service import RoomServer
import test_server as support
class Writer:
    def write(self, frame): pass
    async def drain(self): pass
class ClanParityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state=RoomServer(quiet=True);self.room=Room('CLAN6',owner_id='a')
        self.a=Player('a','A',Writer(),actor_id=10001);self.b=Player('b','B',Writer(),actor_id=10002)
        self.room.players={'a':self.a,'b':self.b};self.clan=self.state.ensure_clan(self.room,self.a,'Clan6')
        self.clan.update(level=25,coin=1000000);self.clan['members']['b']={'role':3,'name':'B'}
    async def asyncTearDown(self): self.state._save_executor.shutdown(wait=True)
    async def buy(self, player, amount):
        return await self.state.handle_clan_interaction(self.room,player,None,{'data':f'281,{amount},10,1,0,0,-1,'},{},'clan_store_buy')
    async def test_two_stale_buyers_do_not_pay_for_clipped_items(self):
        self.clan['items']={'281':{'item':281,'quantity':32740,'expire':-1}}
        self.assertTrue(await self.buy(self.a,20));self.assertFalse(await self.buy(self.b,20))
        self.assertEqual((self.clan['items']['281']['quantity'],self.clan['coin']),(32760,999800))
    async def test_exact_capacity_and_rejected_boundaries(self):
        for current,amount,accepted in [(0,32767,True),(31767,1000,True),(31768,1000,False),(32767,1,False),(0,32768,False),(0,0,False),(0,-1,False)]:
            with self.subTest(current=current,amount=amount):
                self.clan['coin']=1000000;self.clan['items']={'281':{'item':281,'quantity':current,'expire':-1}}
                self.assertEqual(await self.buy(self.a,amount),accepted)
                self.assertEqual(self.clan['items']['281']['quantity'],current+(amount if accepted else 0))
                self.assertEqual(self.clan['coin'],1000000-(amount*10 if accepted else 0))
    async def test_explicit_self_dispatch_creates_durable_obligation(self):
        self.clan['items']={'423':{'item':423,'quantity':1,'expire':259200000,'options':'0=100'}}
        await self.state.interaction(self.room,self.a,{'kind':'clan_item_send','target_actor':0,'data':'423'})
        self.assertNotIn('423',self.clan['items']);events=list(self.room.pending_deliveries['a'].values());self.assertEqual(len(events),1)
        self.assertEqual(events[0]['data'],'423,1,0,0,0,259200000,0=100')
    async def test_self_withdrawal_still_requires_leader_or_deputy(self):
        self.clan['members']['b']['role']=0;self.clan['items']={'281':{'item':281,'quantity':1}}
        await self.state.interaction(self.room,self.b,{'kind':'clan_item_send','target_actor':0,'data':'281'})
        self.assertEqual(self.clan['items']['281']['quantity'],1);self.assertEqual(self.room.pending_deliveries,{})
    async def test_other_zero_target_interactions_not_reinterpreted_as_self(self):
        await self.state.interaction(self.room,self.a,{'kind':'trade_invite','target_actor':0})
        self.assertEqual(self.room.trades,{})
class ClanWireTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.RoomServerTest.asyncSetUp
    asyncTearDown=support.RoomServerTest.asyncTearDown
    connect=support.RoomServerTest.connect
    async def test_real_binary_tcp_stale_purchase_then_self_delivery(self):
        ar,aw,aid=await self.connect('ClanA');br,bw,bid=await self.connect('ClanB')
        async def receive(reader):
            import asyncio
            return await asyncio.wait_for(support.receive(reader),3)
        try:
            await support.send(aw,{'cmd':'create','room':'CLAN6'});await receive(ar)
            await support.send(bw,{'cmd':'join','room':'CLAN6'});await receive(br);await receive(ar)
            room=self.state.rooms['CLAN6'];a=room.players[aid];b=room.players[bid];clan=self.state.ensure_clan(room,a,'Clan6')
            clan.update(level=25,coin=1000000);clan['members'][bid]={'role':3,'name':'ClanB'}
            clan['items']={'281':{'item':281,'quantity':32740,'expire':-1}}
            request={'cmd':'interaction','kind':'clan_store_buy','data':'281,20,100,1,0,0,-1,'}
            await support.send(aw,request);self.assertEqual((await receive(ar))['type'],'clan_state');await receive(br)
            await support.send(bw,request);self.assertEqual((await receive(br))['code'],'clan_item_quantity_limit')
            self.assertEqual((clan['items']['281']['quantity'],clan['coin']),(32760,998000))
            await support.send(aw,{'cmd':'interaction','kind':'clan_item_send','target_actor':0,'data':'281'})
            delivery=await receive(ar);self.assertEqual(delivery['type'],'clan_item_delivery');await receive(ar);await receive(br)
            self.assertEqual(clan['items']['281']['quantity'],32759)
            await support.send(aw,{'cmd':'interaction','kind':'delivery_ack','data':delivery['delivery_id']})
            import asyncio
            for i in range(100):
                if aid not in room.pending_deliveries:break
                await asyncio.sleep(.01)
            self.assertNotIn(aid,room.pending_deliveries)
        finally:
            aw.close();bw.close();await aw.wait_closed();await bw.wait_closed()
if __name__=='__main__':unittest.main()
