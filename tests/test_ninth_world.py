"""Source leader-only Black Saturday weekly clan-fund settlement."""
import asyncio
import datetime
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from test_seventh_settlement import Fixture
from test_server import receive, send
from nso_server.service import RoomServer

WEEK = (datetime.date(2026, 10, 17) - datetime.date(1970, 1, 1)).days
NOW = datetime.datetime(2026, 10, 23, 12, tzinfo=datetime.timezone.utc).timestamp()


class NinthWorldTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = patch('nso_server.mixins.interaction_clan.time.time', return_value=NOW)
        self.clock.start()
        self.f = Fixture()
        self.clan = self.f.clan()

    async def asyncTearDown(self):
        self.f.close()
        self.clock.stop()

    async def claim(self, who='self', week=WEEK, rank=1):
        await self.f.call('clan_black_saturday_claim', who, data=str(week) + ',' + str(rank))

    async def test_only_actual_clan_leader_role_four_can_claim(self):
        for role in (0, 1, 2, 3):
            self.clan['members']['peer']['role'] = role
            await self.claim('peer')
            self.assertEqual(self.f.last('peer', 'error')['code'], 'clan_role_denied')
            self.assertEqual(self.clan['coin'], 1000000)
        self.clan['members']['peer']['role'] = 4
        await self.claim('peer')
        self.assertEqual(self.f.last('peer', 'error')['code'], 'clan_role_denied')
        self.clan['members']['self']['role'] = 3
        await self.claim()
        self.assertEqual(self.f.last('self', 'error')['code'], 'clan_role_denied')
        self.clan['members']['self']['role'] = 4
        await self.claim()
        self.assertEqual(self.clan['coin'], 101000000)

    async def test_fixed_source_reward_table_not_requested_amount(self):
        for index, (rank, amount) in enumerate(((1,100000000),(2,70000000),(3,50000000),(4,20000000),(10,20000000))):
            before = self.clan['coin']
            await self.claim(week=WEEK-index*7, rank=rank)
            event = self.f.last('self', 'activity_state')
            self.assertEqual((event['amount'], self.clan['coin']), (amount, before+amount))
            self.assertEqual(event['phase'], 'black_saturday_claim')
        for raw in ('0,1', str(WEEK)+',0',str(WEEK)+',11',str(WEEK+7)+',1',str(WEEK-1)+',1',str(WEEK)+',1,999999999'):
            before = self.clan['coin']
            await self.f.call('clan_black_saturday_claim', data=raw)
            self.assertEqual(self.f.last('self','error')['code'], 'black_saturday_claim_invalid')
            self.assertEqual(self.clan['coin'], before)

    async def test_retry_duplicate_ack_and_snapshot_pay_once(self):
        await asyncio.gather(self.claim(), self.claim())
        event = self.f.last('self','activity_state')
        ident = event['delivery_id']
        self.assertEqual(self.clan['coin'],101000000)
        self.assertEqual(len(self.f.room.pending_deliveries['self']),1)
        snap = self.f.server._snapshot_state()['worlds']['FIX7']
        self.assertEqual(snap['clans']['fix7']['black_saturday_rewards'][str(WEEK)]['amount'],100000000)
        self.assertIn(ident,snap['pending_deliveries']['self'])
        for _ in range(2):
            await self.f.call('delivery_ack',data=ident+'|BS:'+ident)
            self.assertEqual(self.f.last('self','activity_state')['phase'],'receipt_ack')
        await self.claim()
        self.assertEqual(self.f.last('self','activity_state')['delivery_id'],ident)
        self.assertEqual(self.clan['coin'],101000000)

    async def test_clan_week_identity_survives_leader_replacement(self):
        await self.claim()
        self.clan['leader_id']='peer'
        self.clan['members']['peer']['role']=4
        self.clan['members']['self']['role']=0
        await self.claim('peer')
        self.assertEqual(self.clan['coin'],101000000)
        self.assertEqual(set(self.clan['black_saturday_rewards'][str(WEEK)]['claims']),{'self','peer'})
        await self.claim('peer',rank=2)
        self.assertEqual(self.f.last('peer','error')['code'],'black_saturday_claim_invalid')
        self.assertEqual(self.clan['coin'],101000000)

    async def test_actual_other_clan_is_not_merged_by_player_or_alias(self):
        await self.claim()
        self.clan['members'].pop('peer')
        other=self.f.server.ensure_clan(self.f.room,self.f.players['peer'],'Fix7')
        self.assertEqual(other['name'],'Fix7_1')
        await self.claim('peer')
        self.assertEqual((self.clan['coin'],other['coin']),(101000000,100000000))
        self.assertIn(str(WEEK),other['black_saturday_rewards'])

    async def test_no_clan_and_cap_rejection_leave_no_award_decision(self):
        self.clan['members'].pop('outside')
        await self.claim('outside')
        self.assertEqual(self.f.last('outside','error')['code'],'clan_required')
        self.clan['coin']=1950000000
        await self.claim()
        self.assertEqual(self.f.last('self','error')['code'],'black_saturday_fund_limit')
        self.assertFalse(self.clan.get('black_saturday_rewards'))
        self.assertNotIn('self',self.f.room.pending_deliveries)
        self.clan['coin']=1900000000
        await self.claim()
        self.assertEqual(self.clan['coin'],2000000000)

    async def test_flush_failure_keeps_staged_obligation_and_no_success_reply(self):
        async def failed(): raise OSError('INJECTED_STATE_WRITE_FAILURE')
        with patch.object(self.f.server,'flush_state',failed):
            with self.assertRaises(OSError): await self.claim()
        self.assertFalse(self.f.players['self'].writer.events)
        self.assertEqual(self.clan['coin'],101000000)
        self.assertEqual(len(self.f.room.pending_deliveries['self']),1)
        await self.claim()
        self.assertEqual(self.clan['coin'],101000000)
        self.assertEqual(self.f.last('self','activity_state')['category'],1)

    async def test_socket_failure_stages_both_coin_and_delivery_before_send(self):
        async def failed(*args): raise ConnectionError('INJECTED_DECISION_SOCKET_FAILURE')
        with patch('nso_server.mixins.interaction_clan.send_json',failed): await self.claim()
        self.assertEqual(self.clan['coin'],101000000)
        self.assertEqual(len(self.f.room.pending_deliveries['self']),1)
        await self.claim()
        self.assertEqual(self.clan['coin'],101000000)


class NinthWorldWireTest(unittest.IsolatedAsyncioTestCase):
    async def test_binary_tcp_leader_reward_retry_ack_and_shared_snapshot(self):
        state=RoomServer(quiet=True)
        listener=await asyncio.start_server(state.handle,'127.0.0.1',0)
        reader,writer=await asyncio.open_connection('127.0.0.1',listener.sockets[0].getsockname()[1])
        try:
            welcome=await receive(reader)
            await send(writer,{'cmd':'hello','name':'NinthWorld'})
            await receive(reader)
            await send(writer,{'cmd':'create','room':'NINTHWORLD'})
            await receive(reader)
            room=state.rooms['NINTHWORLD'];player=room.players[welcome['player_id']]
            clan=state.ensure_clan(room,player,'WireNinth');clan['coin']=1000000
            week=int((time.time()+7*3600)//86400)-14
            week-=((week-2)%7)
            async def event(kind,phase=None):
                while True:
                    value=await asyncio.wait_for(receive(reader),3)
                    if value['type']==kind and (phase is None or value.get('phase')==phase):return value
            first=None
            for _ in range(2):
                await send(writer,{'cmd':'interaction','kind':'clan_black_saturday_claim','data':str(week)+',1'})
                decision=await event('activity_state','black_saturday_claim')
                if first is None:first=decision
                self.assertEqual(decision['delivery_id'],first['delivery_id'])
                shared=await event('clan_state')
                self.assertEqual(shared['amount'],101000000)
            ident=first['delivery_id']
            for _ in range(2):
                await send(writer,{'cmd':'interaction','kind':'delivery_ack','data':ident+'|BS:'+ident})
                self.assertEqual((await event('activity_state','receipt_ack'))['data'],'BS:'+ident)
            self.assertEqual(clan['coin'],101000000)
            self.assertFalse(room.pending_deliveries)
        finally:
            writer.close();await writer.wait_closed();await asyncio.sleep(.05)
            listener.close();await listener.wait_closed();state._save_executor.shutdown(wait=True)


async def export_fixtures(path):
    path=Path(path);path.mkdir(parents=True,exist_ok=True);f=Fixture()
    def save(name,event):(path/(name+'.json')).write_text(json.dumps(event,ensure_ascii=False,separators=(',',':')),encoding='utf8')
    try:
        clan=f.clan();save('clan-before',f.server.clan_payload(f.room,clan))
        with patch('nso_server.mixins.interaction_clan.time.time',return_value=NOW):
            await f.call('clan_black_saturday_claim',data=str(WEEK)+',1')
        event=f.last('self','activity_state');save('claim-accepted',event)
        save('clan-after',f.server.clan_payload(f.room,clan))
        await f.call('delivery_ack',data=event['delivery_id']+'|BS:'+event['delivery_id'])
        save('claim-ack',f.last('self','activity_state'))
    finally:f.close()
    print('NINTH_WORLD_FIXTURES_EXPORTED WEEK='+str(WEEK))


if __name__=='__main__':
    import sys
    if len(sys.argv)==3 and sys.argv[1]=='--fixtures':asyncio.run(export_fixtures(sys.argv[2]))
    else:unittest.main()
