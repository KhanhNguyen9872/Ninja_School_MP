"""Session-bound dungeon/territory, cancellation and seller reservation tests."""
import asyncio,json,time,unittest,secrets
from pathlib import Path
from unittest.mock import patch
from nso_server.models import Player,Room
from nso_server.service import RoomServer
from nso_server.protocol import _decode_value,EVENT_TYPES
class Writer:
 def __init__(self):self.events=[]
 def write(self,b):
  p,_=_decode_value(b[8:]);p['type']=EVENT_TYPES[b[3]-64];self.events.append(p)
 async def drain(self):pass
class Fixture:
 def __init__(self):
  self.server=RoomServer(quiet=True);self.room=Room('FIX7',owner_id='self')
  self.players={i:Player(i,i.title(),Writer(),actor_id=10001+n) for n,i in enumerate(('self','peer','outside'))}
  self.room.players=self.players;self.server.rooms[self.room.room_id]=self.room
  for p in self.players.values():p.appearance={'level':100,'class_id':1};p.map_id=22
 def close(self):self.server._save_executor.shutdown(wait=True)
 async def call(self,kind,who='self',target=None,data=''):
  return await self.server.interaction(self.room,self.players[who],{'kind':kind,'target_actor':self.players[target].actor_id if target else 0,'data':data})
 def last(self,who,type):return next(e for e in reversed(self.players[who].writer.events) if e['type']==type)
 async def dungeon(self):
  self.room.parties['self']=['self','peer'];await self.call('dungeon_open',data='6');return self.last('peer','dungeon_admit')
 def clan(self):
  c=self.server.ensure_clan(self.room,self.players['self'],'Fix7');c.update(level=10,coin=1000000,exp=500)
  for i in ('peer','outside'):c['members'][i]={'name':i,'role':0,'class_id':1,'level':100,'points':0}
  return c
 async def trade(self,cancel=False):
  await self.call('trade_invite',target='peer')
  await self.call('trade_accept','peer',target='self')
  for a,b,v in [('self','peer','1000|'),('peer','self','200|0,96,1,0,0,-1,0')]:await self.call('trade_offer',a,b,v)
  for a,b in [('self','peer'),('peer','self')]:await self.call('trade_confirm',a,b)
  pre=self.last('self','trade_prepare')
  if cancel:await self.call('trade_cancel','self',None,'')
  else:
   for a,b in [('self','peer'),('peer','self')]:await self.call('trade_ready',a,b,pre['transaction'])
  return pre
class SeventhServerTest(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):self.f=Fixture()
 async def asyncTearDown(self):self.f.close()
 async def test_dungeon_only_admitted_present_member_once(self):
  f=self.f;admit=await f.dungeon();f.players['peer'].map_id=157
  await f.call('dungeon_finish',data='5,54,3,'+admit['data'])
  self.assertFalse(any(e['type']=='dungeon_finish' for e in f.players['outside'].writer.events))
  event=f.last('peer','dungeon_finish');self.assertEqual(event['data'],admit['data']);self.assertIn(event['delivery_id'],f.room.pending_deliveries['peer'])
  await f.call('dungeon_finish',data='5,54,3,'+admit['data']);self.assertEqual(len(f.room.pending_deliveries['peer']),1)
 async def test_dungeon_absent_member_and_unbound_result_rejected(self):
  f=self.f;admit=await f.dungeon();await f.call('dungeon_finish',data='5,54,3,wrong');self.assertEqual(f.last('self','error')['code'],'dungeon_result_invalid')
  f.players['peer'].map_id=22;await f.call('dungeon_finish',data='5,54,3,'+admit['data']);self.assertNotIn('peer',f.room.pending_deliveries)
 async def test_territory_quota_invitation_reentry_close(self):
  f=self.f;c=f.clan();await f.call('clan_territory_open');t=c['territory'];ident=t['id'];deadline=t['deadline']
  self.assertEqual(t['entries'],0);self.assertFalse(any(e['type']=='clan_territory' for e in f.players['peer'].writer.events))
  await f.call('clan_territory_open');self.assertEqual(t['deadline'],deadline);self.assertEqual(t['entries'],0)
  await f.call('clan_territory_open','peer');self.assertEqual(f.last('peer','error')['code'],'clan_territory_denied')
  await f.call('clan_territory_invite',target='peer');await f.call('clan_territory_open','peer');self.assertIn('peer',t['members'])
  await f.call('clan_territory_sync',data=ident+',1,'+str(int(time.time()*1000)+3500000));await f.call('clan_territory_open','peer');self.assertEqual(f.last('peer','clan_territory')['category'],1)
  await f.call('clan_territory_open','outside');self.assertEqual(f.last('outside','error')['code'],'clan_territory_denied')
  await f.call('clan_territory_close',data=ident+',1,0');self.assertFalse(t['active'])
  await f.call('clan_territory_open');self.assertEqual(f.last('self','error')['code'],'clan_territory_no_entries')
  self.assertFalse(any(e.get('type')=='clan_territory' for p in f.room.pending_deliveries.values() for e in p.values()))
 async def test_territory_gmt7_daily_and_permit(self):
  f=self.f;c=f.clan();t=f.server.refresh_clan_territory(c);t.update(entries=0,use_card=0,active=False,day=t['day']-1)
  f.server.refresh_clan_territory(c);self.assertEqual((t['entries'],t['use_card']),(1,1));t['entries']=0
  c['items']={'281':{'item':281,'quantity':1}};await f.call('clan_item_use',data='281');self.assertEqual((t['entries'],t['use_card']),(1,0));self.assertNotIn('281',c['items'])
 async def test_market_reclaim_waits_for_buyer_and_seller_ack(self):
  f=self.f;expiry=int(time.time()*1000)+60000;raw=f'0,94,1,0,0,1000,{expiry},-1';await f.call('shinwa_publish',data=raw);pid=next(iter(f.room.shinwa));await f.call('shinwa_buy','peer',None,str(pid));buy=f.last('peer','shinwa_sold')
  with patch('nso_server.mixins.interaction_gameplay.time.time',return_value=(expiry+1)/1000):await f.call('shinwa_reclaim',data=raw)
  self.assertEqual(f.last('self','error')['code'],'shinwa_settling')
  await f.call('shinwa_publish',data=raw);self.assertEqual(f.last('self','error')['code'],'shinwa_settling')
  await f.call('delivery_ack','peer',None,buy['delivery_id']);sell=f.last('self','shinwa_sold');self.assertEqual(sell['data'],raw)
  with patch('nso_server.mixins.interaction_gameplay.time.time',return_value=(expiry+1)/1000):await f.call('shinwa_reclaim',data=raw)
  self.assertEqual(f.last('self','error')['code'],'shinwa_settling')
 async def test_market_unsold_reclaim_same_receipt_retry(self):
  f=self.f;raw=f'0,94,1,0,0,1000,{int(time.time()*1000)-1},-1';await f.call('shinwa_reclaim',data=raw);first=f.last('self','shinwa_removed');await f.call('shinwa_reclaim',data=raw)
  self.assertEqual(first['delivery_id'],f.last('self','shinwa_removed')['delivery_id']);self.assertEqual(len(f.room.pending_deliveries['self']),1)
 async def test_cancel_is_durable_and_transaction_bound(self):
  f=self.f;pre=await f.trade(cancel=True);event=f.last('self','trade_cancel');self.assertEqual(event['transaction'],pre['transaction']);self.assertIn(event['delivery_id'],f.room.pending_deliveries['self']);self.assertEqual(f.room.trades,{})
 async def test_cancel_after_commit_cannot_revoke_obligations(self):
  f=self.f;pre=await f.trade();await f.call('trade_cancel');self.assertEqual(f.last('self','error')['code'],'trade_not_open')
  self.assertEqual(sorted(f.room.pending_deliveries),['peer','self'])
async def export_fixtures(path):
 path=Path(path);path.mkdir(parents=True,exist_ok=True)
 def save(n,v):(path/(n+'.json')).write_text(json.dumps(v,ensure_ascii=False,separators=(',',':')),encoding='utf8')
 f=Fixture()
 try:
  ad=await f.dungeon();save('dungeon-admit',ad);f.players['peer'].map_id=157;await f.call('dungeon_finish',data='5,54,3,'+ad['data']);save('dungeon-finish',f.last('peer','dungeon_finish'))
  save('outsider-finish',dict(f.last('peer','dungeon_finish'),owner_id='self'))
  c=f.clan();save('clan-before',f.server.clan_payload(f.room,c));await f.call('clan_territory_open');save('territory-owner',f.last('self','clan_territory'));save('clan-open',f.server.clan_payload(f.room,c))
  await f.call('clan_territory_invite',target='peer');await f.call('clan_territory_open','peer');save('territory-guest',f.last('peer','clan_territory'))
  await f.call('clan_territory_close',data=c['territory']['id']+',0,0');save('clan-closed',f.server.clan_payload(f.room,c))
 finally:f.close()
 f=Fixture()
 try:
  pre=await f.trade();save('trade-open',f.last('self','trade_open'));save('trade-prepare',pre);save('trade-commit',f.last('self','trade_commit'))
 finally:f.close()
 f=Fixture()
 try:await f.trade(cancel=True);save('trade-cancel',f.last('self','trade_cancel'))
 finally:f.close()
 f=Fixture()
 try:
  expiry=int(time.time()*1000)+100;raw=f'0,94,1,0,0,1000,{expiry},-1';await f.call('shinwa_publish',data=raw);await f.call('shinwa_buy','peer',None,str(next(iter(f.room.shinwa))));buy=f.last('peer','shinwa_sold');await f.call('delivery_ack','peer',None,buy['delivery_id']);save('market-seller',f.last('self','shinwa_sold'))
  raw2=f'1,94,1,0,0,1000,{int(time.time()*1000)-1},-1';await f.call('shinwa_reclaim',data=raw2);save('market-reclaim',f.last('self','shinwa_removed'))
 finally:f.close()
 print('SEVENTH_SERVER_FIXTURES_EXPORTED')
if __name__=='__main__':
 import sys
 if len(sys.argv)==3 and sys.argv[1]=='--fixtures':asyncio.run(export_fixtures(sys.argv[2]))
 else:unittest.main()
