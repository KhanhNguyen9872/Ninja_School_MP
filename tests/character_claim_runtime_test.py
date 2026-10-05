import asyncio,sys,struct,json,time
from pathlib import Path
root=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
from nso_server.service import RoomServer
from nso_server.protocol import _encode_value,_decode_value,EVENT_TYPES
from nso_server.models import Player,Room
import tempfile
out=Path(tempfile.mkdtemp(prefix="nso-character-claim-"))
async def read(reader):
 header=await asyncio.wait_for(reader.readexactly(8),5)
 raw=await reader.readexactly(struct.unpack('>I',header[4:])[0])
 data,n=_decode_value(raw);assert n==len(raw);data["type"]=EVENT_TYPES[header[3]-64]
 return data,header+raw
async def send(writer,opcode,data):
 raw=_encode_value(data);writer.write(b'NS'+bytes((2,opcode))+struct.pack('>I',len(raw))+raw);await writer.drain()
async def until(reader,types):
 for _ in range(20):
  data,raw=await read(reader)
  if data.get('type') in types:return data,raw
 raise AssertionError('no target event')
async def main():
 server=RoomServer();server.log=lambda *a,**kw:None
 listener=await asyncio.start_server(server.handle,'127.0.0.1',0)
 port=listener.sockets[0].getsockname()[1];clients=[]
 try:
  for i in range(4):
   print('TCP_OPEN='+str(i),flush=True)
   reader,writer=await asyncio.open_connection('127.0.0.1',port);welcome,_=await read(reader);print('WELCOME='+str(i),flush=True)
   await send(writer,2,{'room':'ISO'+str(i),'password':'','cheat_enabled':False});await until(reader,{'room_joined','error'});print('JOINED='+str(i),flush=True)
   clients.append((reader,writer,welcome['player_id']))
  async def claim(index,identity):
   r,w,_=clients[index];await send(w,13,{'kind':'character_claim','text':identity,'target_actor':0,'damage':0,'skill':0});return await until(r,{'character_claimed','error'})
  identity='0123456789abcdef';other='fedcba9876543210'
  a,raw=await claim(0,identity);b,busy=await claim(1,identity)
  if not hasattr(server,'character_leases'):
   assert len(server.players)==4 and a['type']==b['type']=='error'
   print('BASELINE_NO_STABLE_CHARACTER_CLAIM active_members=4 requests=unsupported')
   print('SERVER_CHARACTER_BASELINE_RESULT=PASS');return
  assert a['type']=='character_claimed' and b['code']=='character_online'
  (out/'claim_ack.bin').write_bytes(raw);(out/'claim_busy.bin').write_bytes(busy)
  print('SAME_CHARACTER_ACROSS_ROOMS first=granted second=blocked')
  b,_=await claim(1,other);assert b['type']=='character_claimed'
  print('DIFFERENT_CHARACTERS_ACROSS_ROOMS both=granted')
  a,_=await claim(0,identity);assert a['type']=='character_claimed'
  print('SAME_CONNECTION_CLAIM_IDEMPOTENT=PASS')
  values=await asyncio.gather(claim(2,'aaaaaaaaaaaaaaaa'),claim(3,'aaaaaaaaaaaaaaaa'))
  assert sum(v[0]['type']=='character_claimed' for v in values)==1
  assert sum(v[0].get('code')=='character_online' for v in values)==1
  print('SIMULTANEOUS_CHARACTER_CLAIMS exactly_one=granted')
  bad,_=await claim(1,'bad-key');assert bad['code']=='character_id_invalid'
  old=server.players[clients[0][2]];clients[0][1].close();await clients[0][1].wait_closed()
  deadline=time.monotonic()+3
  while clients[0][2] in server.players and time.monotonic()<deadline:await asyncio.sleep(.01)
  b,_=await claim(1,identity);assert b['code']=='character_online'
  assert server.character_leases[other][0] is server.players[clients[1][2]]
  print('DISCONNECT_SAVE_GRACE blocks_immediate_reentry=PASS')
  await asyncio.sleep(8.1)
  b,_=await claim(1,identity);assert b['type']=='character_claimed'
  successor=server.players[clients[1][2]]
  await server.disconnect(old)
  assert server.character_leases[identity][0] is successor
  print('GRACE_EXPIRES_AND_OLD_CLOSE_CANNOT_RELEASE_SUCCESSOR=PASS')
  await send(clients[1][1],13,{'kind':'character_release','text':identity,'target_actor':0,'damage':0,'skill':0})
  deadline=time.monotonic()+3
  while identity in server.character_leases and time.monotonic()<deadline:await asyncio.sleep(.01)
  assert identity not in server.character_leases
  print('OWNER_ONLY_RELEASE_AFTER_SAVE=PASS')
  print('SERVER_CHARACTER_MODIFIED_RESULT=PASS')
 finally:
  for _,writer,_ in clients:
   writer.close()
   try:await asyncio.wait_for(writer.wait_closed(),2)
   except (OSError,asyncio.TimeoutError):pass
  listener.close();await asyncio.wait_for(listener.wait_closed(),2);await asyncio.sleep(.03)
  server._save_executor.shutdown(wait=True)
asyncio.run(asyncio.wait_for(main(),30))
