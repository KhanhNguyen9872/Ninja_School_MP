"""Source Hibikou is status-only for real players, not generic HP damage."""
from pathlib import Path
import argparse,asyncio,copy,json,sys,time,unittest
from unittest.mock import patch
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import RoomServer
from nso_server.models import Player
from test_server import send,receive

def profile():
    return dict(class_id=3,level=100,weapon=12,max_hp=1000,mp=1000,max_mp=2000,
                damage=2000,exactly=100,miss=150,fatal=0,
                combat=dict(v=1,damage2=1800,skill=24,attack_type=1,dx=70,dy=70,
                            mana=40,cooldown=55000,bind_ms=3000))
class Writer:
    def write(self,b):pass
    async def drain(self):pass

class TenthCombatDomainTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.s=RoomServer(quiet=True)
        self.a=Player('a','A',Writer(),map_id=2,x=100,y=100,hp=1000)
        self.b=Player('b','B',Writer(),map_id=2,x=120,y=100,hp=1000)
        for player in (self.a,self.b):player.appearance=self.s.clean_appearance(profile())
    async def test_bind_before_damage_miss_crit_and_stealth_consumption(self):
        self.a.appearance['combat']['stealth_ms']=5000
        self.a.last_pvp_fatal=True
        self.b.revive_protected_until=time.monotonic()+5
        with patch.object(self.s,'authoritative_pvp_damage',side_effect=AssertionError('DAMAGE_BRANCH')):
            self.assertEqual(await self.s.validate_pvp_attack(self.a,self.b,24),0)
        self.assertEqual(self.b.hp,1000);self.assertFalse(self.a.last_pvp_fatal)
        self.assertGreater(self.b.pvp_bound_until,time.monotonic()+2.9)
        self.assertEqual(self.a.appearance['mp'],960)
        self.assertEqual(self.a.appearance['combat']['stealth_ms'],5000)
    async def test_cooldown_never_renews_binding_or_charges_again(self):
        self.assertEqual(await self.s.validate_pvp_attack(self.a,self.b,24),0)
        deadline=self.b.pvp_bound_until
        self.assertEqual(await self.s.validate_pvp_attack(self.a,self.b,24),-1)
        self.assertEqual(self.b.pvp_bound_until,deadline);self.assertEqual(self.a.appearance['mp'],960)
    async def test_bound_target_still_can_cast(self):
        self.assertEqual(await self.s.validate_pvp_attack(self.a,self.b,24),0)
        self.assertEqual(await self.s.validate_pvp_attack(self.b,self.a,24),0)
        self.assertGreater(self.a.pvp_bound_until,time.monotonic())
    async def test_profile_bounds_missing_duration_and_normal_damage_control(self):
        p=profile();p['combat']['bind_ms']=99999999
        self.assertEqual(self.s.clean_appearance(p)['combat']['bind_ms'],600000)
        self.a.appearance['combat']['bind_ms']=0
        self.assertEqual(await self.s.validate_pvp_attack(self.a,self.b,24),-1)
        self.assertEqual(self.a.appearance['mp'],1000);self.assertFalse(self.a.pvp_ready_at)
        self.a.appearance['combat']['skill']=1
        with patch('nso_server.mixins.combat.secrets.randbelow',return_value=0):
            self.assertEqual(await self.s.validate_pvp_attack(self.a,self.b,1),108)
        self.assertEqual(self.b.pvp_bound_until,0)
    async def test_original_actor_range_mana_and_map_gates_still_apply(self):
        for mode in ('range','mana','map','zone','dead','locked','invisible','weapon'):
            a,b=copy.deepcopy(self.a),copy.deepcopy(self.b)
            if mode=='range':b.x=171
            elif mode=='mana':a.appearance['mp']=39
            elif mode=='map':a.map_id=b.map_id=22
            elif mode=='zone':b.zone_id=1
            elif mode=='dead':b.hp=0
            elif mode=='locked':a.appearance['combat']['locked_ms']=10000
            elif mode=='invisible':b.appearance['combat']['invisible_ms']=10000
            elif mode=='weapon':a.appearance['weapon']=-1
            self.assertEqual(await self.s.validate_pvp_attack(a,b,24),-1,mode)
            self.assertEqual(a.appearance['mp'],39 if mode=='mana' else 1000,mode)
            self.assertEqual(b.pvp_bound_until,0,mode)

async def export_wire(fixtures,native=None):
    fixtures=Path(fixtures);fixtures.mkdir(parents=True,exist_ok=True)
    native=native or profile()
    for kind in ('pvp','duel'):
        s=RoomServer(quiet=True);server=await asyncio.start_server(s.handle,'127.0.0.1',0);port=server.sockets[0].getsockname()[1];writers=[];trace=[]
        async def event(r,name):
            for _ in range(40):
                e=await asyncio.wait_for(receive(r),5);trace.append(e)
                if e['type']==name:return e
            raise AssertionError(name)
        async def connect(name):
            r,w=await asyncio.open_connection('127.0.0.1',port);writers.append(w);welcome=await event(r,'welcome');await send(w,dict(cmd='hello',name=name));await event(r,'hello_ok');return r,w,welcome['player_id']
        async def barrier(r,w):
            await send(w,dict(cmd='snapshot'));return await event(r,'snapshot')
        try:
            ar,aw,aid=await connect('TenthOwner');br,bw,bid=await connect('TenthGuest')
            await send(aw,dict(cmd='create',room='TENTHCOMBAT'));await event(ar,'room_joined');await send(bw,dict(cmd='join',room='TENTHCOMBAT'));await event(br,'room_joined');await event(ar,'player_join')
            for w,r,name in ((aw,br,'TenthOwner'),(bw,ar,'TenthGuest')):
                await send(w,dict(native,cmd='appearance',name=name));await event(r,'player_appearance')
            for w,r,x in ((aw,br,100),(bw,ar,120)):
                await send(w,dict(cmd='state',map=2,zone=0,x=x,y=100,hp=1000));await event(r,'player_state')
            a=s.rooms['TENTHCOMBAT'].players[aid];b=s.rooms['TENTHCOMBAT'].players[bid]
            if kind=='pvp':
                await send(aw,dict(cmd='interaction',kind='cuu_sat',target_actor=b.actor_id));await event(ar,'cuu_sat_start');await event(br,'cuu_sat_start')
            else:
                await send(aw,dict(cmd='interaction',kind='duel_invite',target_actor=b.actor_id));await event(br,'duel_invite')
                await send(bw,dict(cmd='interaction',kind='duel_accept',target_actor=a.actor_id));await event(ar,'duel_start');await event(br,'duel_start')
            with patch('nso_server.mixins.combat.secrets.randbelow',return_value=0):
                await send(aw,dict(cmd='interaction',kind=kind+'_attack',target_actor=b.actor_id,damage=2000000000,skill=24));hit=await event(br,kind+'_hit');await event(ar,kind+'_hit')
            assert b.hp==1000 and hit['hp']==1000 and hit['damage']==0,hit
            assert hit['template']==18 and 0<hit['quantity']<=3000,hit
            assert a.appearance['mp']==960 and b.pvp_bound_until>time.monotonic()
            (fixtures/(kind+'.json')).write_text(json.dumps(hit,ensure_ascii=False,separators=(',',':')),encoding='utf8')
            deadline=b.pvp_bound_until
            await send(aw,dict(cmd='interaction',kind=kind+'_attack',target_actor=b.actor_id,skill=24));err=await event(ar,'error')
            assert err['code']=='skill_cooldown' and a.appearance['mp']==960 and b.pvp_bound_until==deadline
            # Ordinary state can still report HP while an in-flight move is clamped.
            await send(bw,dict(cmd='state',map=2,zone=0,x=121,y=100,hp=990));err=await event(br,'error');await barrier(br,bw)
            assert err['code']=='movement_locked' and b.x==120 and b.hp==990
            # A same-map map command and an appearance refresh do not erase bind.
            await send(bw,dict(native,cmd='appearance',name='TenthGuest'));await event(ar,'player_appearance')
            await send(bw,dict(cmd='map',map=2,zone=0,x=122,y=100));err=await event(br,'error');await barrier(br,bw)
            assert err['code']=='movement_locked' and b.x==120 and b.pvp_bound_until==deadline
            await barrier(br,bw);assert b.pvp_bound_until==deadline
            # Source type14 is not a stun: the victim can cast while bound.
            await send(bw,dict(cmd='interaction',kind=kind+'_attack',target_actor=a.actor_id,skill=24));reverse=await event(ar,kind+'_hit');await event(br,kind+'_hit')
            assert reverse['damage']==0 and a.hp==1000 and b.appearance['mp']==960
            if kind=='pvp':
                await asyncio.sleep(max(0,deadline-time.monotonic())+.03)
                await send(bw,dict(cmd='state',map=2,zone=0,x=121,y=100,hp=990));await barrier(br,bw)
                assert b.x==121 and b.pvp_bound_until<=time.monotonic()
            print('TENTH_TCP_'+kind.upper()+'_SOURCE_BIND HP=1000 DAMAGE=0 MANA=40 COOLDOWN=ON MOVE_LOCK=ON ATTACK_ALLOWED=YES',flush=True)
        finally:
            (fixtures/(kind+'-trace.json')).write_text(json.dumps(trace,ensure_ascii=False,indent=2),encoding='utf8')
            for w in writers:w.close()
            for w in writers:
                try:await w.wait_closed()
                except (OSError,ConnectionError):pass
            server.close();await server.wait_closed()

class TenthCombatWireTest(unittest.IsolatedAsyncioTestCase):
    async def test_actual_tcp_status_only_pvp_and_duel(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:await export_wire(Path(d))

if __name__=='__main__':
    if '--fixtures' in sys.argv:
        p=argparse.ArgumentParser();p.add_argument('--fixtures',type=Path,required=True);p.add_argument('--appearance',type=Path);a=p.parse_args()
        asyncio.run(export_wire(a.fixtures,json.loads(a.appearance.read_text(encoding='utf8')) if a.appearance else None))
    else:unittest.main()
