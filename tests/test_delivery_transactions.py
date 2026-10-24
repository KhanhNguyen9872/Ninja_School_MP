"""Real binary TCP delivery/reconnect plus writer-failure transaction regressions."""
import asyncio
import unittest
import time
import test_server as support
send = support.send
async def receive(reader):
    return await asyncio.wait_for(support.receive(reader), 3)
from nso_server.models import Player, Room
from nso_server.service import RoomServer

class DeliveryTransactionTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.RoomServerTest.asyncSetUp
    asyncTearDown = support.RoomServerTest.asyncTearDown
    connect = support.RoomServerTest.connect

    async def test_market_reservation_retry_deferred_seller_and_tcp_resume(self):
        sr, sw, sid = await self.connect('Seller')
        br, bw, bid = await self.connect('Buyer')
        try:
            await send(sw, {'cmd':'create','room':'TXMARKET'}); await receive(sr)
            await send(bw, {'cmd':'join','room':'TXMARKET'})
            joined=await receive(br); await receive(sr)
            room=self.state.rooms['TXMARKET']
            products=[]
            for slot in (0,1):
                raw=f'{slot},94,1,0,0,1000,{int(time.time()*1000)+600000},-1'
                await send(sw, {'cmd':'interaction','kind':'shinwa_publish','data':raw})
                await receive(sr); products.append((await receive(br))['item_id'])
            await send(bw, {'cmd':'interaction','kind':'shinwa_buy','data':str(products[0])})
            event=await asyncio.wait_for(receive(br),2)
            self.assertEqual(event['type'],'shinwa_sold')
            self.assertNotIn(sid,room.pending_deliveries)
            await send(bw, {'cmd':'interaction','kind':'shinwa_buy','data':str(products[1])})
            retry=await receive(br); err=await receive(br)
            self.assertEqual(retry['delivery_id'],event['delivery_id'])
            self.assertEqual(err['code'],'shinwa_pending')
            self.assertIn(products[1],room.shinwa)
            self.assertEqual(len(room.pending_deliveries[bid]),1)
            bw.close(); await bw.wait_closed()
            self.assertEqual((await asyncio.wait_for(receive(sr),2))['type'],'player_leave')
            br,bw,_=await self.connect('BuyerAgain')
            await send(bw,{'cmd':'resume','player_id':bid,'room':'TXMARKET','resume_token':joined['resume_token']})
            rejoined=await receive(br); replay=await receive(br); await receive(sr)
            self.assertEqual(rejoined['player_id'],bid)
            self.assertEqual(replay['delivery_id'],event['delivery_id'])
            self.assertNotIn(sid,room.pending_deliveries)
            await send(bw,{'cmd':'interaction','kind':'delivery_ack','data':replay['delivery_id']})
            seller=await asyncio.wait_for(receive(sr),2)
            self.assertEqual(seller['transaction'],event['transaction'])
            self.assertNotIn(bid,room.pending_deliveries)
            self.assertEqual(len(room.pending_deliveries[sid]),1)
            await send(bw,{'cmd':'interaction','kind':'delivery_ack','data':replay['delivery_id']})
            self.assertEqual((await receive(br))['code'],'delivery_not_found')
            self.assertEqual(len(room.pending_deliveries[sid]),1)
        finally:
            bw.close();sw.close();await bw.wait_closed();await sw.wait_closed()

    async def test_trade_stages_both_offers_before_any_failing_writer(self):
        class Writer:
            def __init__(self):self.fail=False
            def write(self,b):
                if self.fail:raise ConnectionResetError('fixture disconnected before commit')
            async def drain(self):pass
        state=RoomServer(quiet=True);room=Room('TXTRADE', owner_id='a')
        a=Player('a','A',Writer(),actor_id=10001);b=Player('b','B',Writer(),actor_id=10002)
        room.players={'a':a,'b':b}
        try:
            await state.handle_trade_interaction(room,a,b,{}, {},'trade_invite')
            await state.handle_trade_interaction(room,b,a,{}, {},'trade_accept')
            for x,y,offer in [(a,b,'1000|'),(b,a,'200|0,96,1,0,0,-1,0')]:
                await state.handle_trade_interaction(room,x,y,{'data':offer},{},'trade_offer')
            for x,y in [(a,b),(b,a)]:await state.handle_trade_interaction(room,x,y,{}, {},'trade_confirm')
            trade=next(iter(room.trades.values()));transaction=trade['transaction']
            self.assertFalse(await state.handle_trade_interaction(room,a,b,{'data':'0|'},{},'trade_offer'))
            self.assertEqual(trade['offers']['a'],'1000|')
            await state.handle_trade_interaction(room,a,b,{'data':transaction},{},'trade_ready')
            b.writer.fail=True
            await state.handle_trade_interaction(room,b,a,{'data':transaction},{},'trade_ready')
            self.assertEqual(room.trades,{})
            # Both immutable offers survive the real disk snapshot/reload path.
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory(prefix='nso-tx-state-') as tmp:
                state.state_file = str(Path(tmp)/'state.json')
                state.rooms[room.room_id] = room
                await state.flush_state()
                # Existing policy intentionally discards rooms on server restart.
                # Check disk obligations, not unsupported restarted live rooms.
                import json
                disk = json.loads(Path(state.state_file).read_text(encoding='utf-8'))
                self.assertEqual(disk['worlds'][room.room_id]['pending_deliveries'],
                                 room.pending_deliveries)
                state.state_file = None
            for player,own,peer in [('a','1000|','200|0,96,1,0,0,-1,0'),('b','200|0,96,1,0,0,-1,0','1000|')]:
                events=list(room.pending_deliveries[player].values());self.assertEqual(len(events),1)
                self.assertEqual((events[0]['own_offer'],events[0]['peer_offer']),(own,peer))
                self.assertEqual(events[0]['transaction'],transaction)
            b.writer.fail=False
            self.assertFalse(await state.handle_trade_interaction(room,a,b,{}, {},'trade_accept'))
        finally:state._save_executor.shutdown(wait=True)

if __name__=='__main__':unittest.main()
