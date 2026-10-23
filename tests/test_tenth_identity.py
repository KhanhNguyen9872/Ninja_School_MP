"""Room-lifetime primary/clone IDs must not alias different players."""
import asyncio,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import RoomServer,Room,EVENT_OPCODES,_decode_value,password_digest
from nso_server.models import Player
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_server import send,receive

class Writer:
    def __init__(self):self.frames=[]
    def write(self,b):self.frames.append(b)
    async def drain(self):pass
    def close(self):pass
    async def wait_closed(self):pass
    def events(self):
        result=[]
        for f in self.frames:
            e,_=_decode_value(f[8:]);e['type']=next(k for k,v in EVENT_OPCODES.items() if v==f[3]);result.append(e)
        return result

class TenthIdentityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s=RoomServer(quiet=True);self.r=Room('R');self.s.rooms['R']=self.r
    async def join(self,identity,name='Guest'):
        p=Player(identity,name,Writer());self.s.players[identity]=p;await self.s.join(p,'R');return p
    async def test_real_hash_collision_does_not_replace_target_index(self):
        a=await self.join('00000001abcd','A');b=await self.join('2faf0801abcd','B')
        self.assertEqual(self.s.actor_id_for(a.player_id),self.s.actor_id_for(b.player_id))
        self.assertNotEqual(a.actor_id,b.actor_id);self.assertIs(self.s.target_actor(self.r,a.actor_id),a);self.assertIs(self.s.target_actor(self.r,b.actor_id),b)
    async def test_real_primary_cannot_reuse_reserved_clone_id(self):
        a=await self.join('00000001abcd','A');cid=self.s.actor_id_for(a.player_id+'#clone');b=await self.join(format(cid-1000000000,'08x')+'abcd','B')
        self.assertEqual(self.s.actor_id_for(b.player_id),cid);self.assertNotEqual(b.actor_id,cid);self.assertEqual(self.s.room_actor_id(self.r,a.player_id+'#clone'),cid)
    async def test_all_colliding_primary_and_clone_preferences_are_unique(self):
        self.s.actor_id_for=lambda identity:1000000001
        players=[await self.join(str(i),'P'+str(i)) for i in range(4)]
        ids=[p.actor_id for p in players]+[self.s.room_actor_id(self.r,p.player_id+'#clone') for p in players]
        self.assertEqual(len(set(ids)),8);self.assertEqual([self.s.room_actor_id(self.r,p.player_id) for p in players],[p.actor_id for p in players])
    async def test_guest_leave_reserves_both_ids_and_rejoin_recovers_them(self):
        a=await self.join('00000001abcd','A');b=await self.join('2faf0801abcd','B');old=b.actor_id;clone=self.s.room_actor_id(self.r,b.player_id+'#clone')
        await self.s.disconnect_from_room(b);c=await self.join(format(old-1000000000,'08x')+'eeee','C');self.assertNotEqual(c.actor_id,old)
        await self.s.join(b,'R');self.assertEqual(b.actor_id,old);self.assertEqual(self.s.room_actor_id(self.r,b.player_id+'#clone'),clone);self.assertIs(self.s.target_actor(self.r,a.actor_id),a)
    async def test_no_collision_preserves_legacy_preferences_and_room_scope(self):
        a=await self.join('00000001abcd','A');self.assertEqual(a.actor_id,self.s.actor_id_for(a.player_id));self.assertEqual(self.s.room_actor_id(self.r,a.player_id+'#clone'),self.s.actor_id_for(a.player_id+'#clone'))
        other=Room('OTHER');self.assertEqual(self.s.room_actor_id(other,'another',a.actor_id),a.actor_id)
    async def test_probe_wrap_keeps_signed_wire_range(self):
        self.r.actor_ids['occupied']=1799999999;v=self.s.room_actor_id(self.r,'wrap',1799999999);self.assertEqual(v,1000000000);self.assertLess(v,2147483647)
    async def test_rejected_join_does_not_reserve_or_rebind_ids(self):
        a=await self.join('00000001abcd','A');before=dict(self.r.actor_ids);self.r.password_hash=password_digest('secret');p=Player('2faf0801abcd','Denied',Writer());await self.s.join(p,'R','bad');self.assertEqual(self.r.actor_ids,before);self.assertIs(self.s.target_actor(self.r,a.actor_id),a);self.assertNotIn(p.player_id,self.r.players)
    async def test_joined_socket_resume_preserves_both_room_identities(self):
        a=await self.join('00000001abcd','A');b=await self.join('2faf0801abcd','B');token=next(e['resume_token'] for e in b.writer.events() if e['type']=='room_joined');bid=b.actor_id
        await self.s.disconnect(b);other=Room('S');self.s.rooms['S']=other;p=Player('socketS','OwnerS',Writer());self.s.players[p.player_id]=p;await self.s.join(p,'S');identity=p.player_id;aid=p.actor_id
        await self.s.resume(p,b.player_id,'R',token)
        self.assertEqual((p.player_id,p.room_id,p.actor_id),(identity,'S',aid));self.assertIs(other.players[identity],p);self.assertIs(self.s.target_actor(other,aid),p);self.assertNotIn(b.player_id,self.r.players);self.assertEqual(p.writer.events()[-1]['code'],'resume_already_joined')
        fresh=Player('fresh','Fresh',Writer());self.s.players[fresh.player_id]=fresh;await self.s.resume(fresh,b.player_id,'R',token);self.assertEqual((fresh.player_id,fresh.actor_id,fresh.room_id),(b.player_id,bid,'R'));self.assertIs(other.players[identity],p)
    async def test_binary_tcp_join_clone_snapshot_target_and_resume(self):
        s=RoomServer(quiet=True);s.actor_id_for=lambda identity:1000000001;server=await asyncio.start_server(s.handle,'127.0.0.1',0);port=server.sockets[0].getsockname()[1];writers=[]
        async def event(reader,kind):
            for _ in range(30):
                e=await asyncio.wait_for(receive(reader),3)
                if e['type']==kind:return e
            self.fail('missing '+kind)
        try:
            ar,aw=await asyncio.open_connection('127.0.0.1',port);writers.append(aw);a=await event(ar,'welcome');await send(aw,dict(cmd='create',room='WIRE',name='A'));ja=await event(ar,'room_joined')
            br,bw=await asyncio.open_connection('127.0.0.1',port);writers.append(bw);b=await event(br,'welcome');await send(bw,dict(cmd='join',room='WIRE',name='B'));jb=await event(br,'room_joined');aid=next(p['actor_id'] for p in jb['players'] if p['player_id']==a['player_id']);bid=next(p['actor_id'] for p in jb['players'] if p['player_id']==b['player_id']);self.assertNotEqual(aid,bid)
            await send(aw,dict(cmd='clone',active=True,hp=100));await event(br,'clone_state');await send(bw,dict(cmd='clone',active=True,hp=100));await event(ar,'clone_state');await send(aw,dict(cmd='snapshot'));snap=await event(ar,'snapshot');ids=[p['actor_id'] for p in snap['players']]+[p['clone_actor_id'] for p in snap['clones']];self.assertEqual(len(ids),4);self.assertEqual(len(set(ids)),4)
            await send(bw,dict(cmd='interaction',kind='party_invite',target_actor=aid));inv=await event(ar,'party_invite');self.assertEqual(inv['actor_id'],bid)
            bw.close();await bw.wait_closed();await event(ar,'player_leave');await send(aw,dict(cmd='resume',room='WIRE',player_id=b['player_id'],resume_token=jb['resume_token']));denied=await event(ar,'error');self.assertEqual(denied['code'],'resume_already_joined');self.assertEqual(s.rooms['WIRE'].owner_id,a['player_id']);cr,cw=await asyncio.open_connection('127.0.0.1',port);writers.append(cw);await event(cr,'welcome');await send(cw,dict(cmd='resume',room='WIRE',player_id=b['player_id'],resume_token=jb['resume_token']));jr=await event(cr,'room_joined');self.assertEqual(next(p['actor_id'] for p in jr['players'] if p['player_id']==b['player_id']),bid)
        finally:
            for writer in reversed(writers):writer.close()
            for writer in reversed(writers):
                try:await writer.wait_closed()
                except OSError:pass
            server.close();await server.wait_closed();await asyncio.sleep(.02)

if __name__=='__main__':unittest.main()
