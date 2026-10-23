"""Source-shaped real-peer combat, including actual protocol-v2 TCP fixtures."""
import argparse,asyncio,copy,json,sys,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import RoomServer
from nso_server.models import Player
from test_server import send,receive

def native_profile():
    # Exact material fields of EighthCombatFixProbe.profile's Ken1/point1;
    # the runtime gate replaces this with full native appearanceFields JSON.
    return dict(name='Eighth',class_id=1,level=100,weapon=12,max_hp=1000,max_mp=2000,
                mp=1000,damage=3010,damage_down=0,res_fire=0,res_ice=0,res_wind=0,
                exactly=100,miss=150,fatal=0,equipment_ids=[-1,94]+[-1]*14,
                combat=dict(v=1,damage2=2709,skill=1,attack_type=1,dx=51,dy=18,mana=1,cooldown=500))

class Writer:
    def __init__(self): self.frames=[]
    def write(self,b): self.frames.append(b)
    async def drain(self): pass

class EighthCombatDomainTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s=RoomServer()
        self.a=Player('a','A',Writer(),actor_id=10001,map_id=2,x=100,y=100,hp=1000)
        self.b=Player('b','B',Writer(),actor_id=10002,map_id=2,x=120,y=100,hp=1000)
        for p in (self.a,self.b):p.appearance=self.s.clean_appearance(native_profile())

    async def test_equal_level_source_scale_and_ignore_claimed_damage(self):
        with patch('nso_server.mixins.combat.secrets.randbelow',return_value=0):
            damage=await self.s.validate_pvp_attack(self.a,self.b,1)
        self.assertEqual(damage,162);self.assertEqual(self.a.appearance['mp'],999)
        self.a.appearance['combat']['damage2']=3010
        self.assertLessEqual(self.s.authoritative_pvp_damage(self.a,self.b),180)

    async def test_class_specific_resistance_not_average(self):
        self.a.appearance['combat']['damage2']=3010
        self.b.appearance.update(res_fire=1000,res_ice=3000,res_wind=3000)
        with patch('nso_server.mixins.combat.secrets.randbelow',return_value=0):
            self.assertEqual(self.s.authoritative_pvp_damage(self.a,self.b),120)
        self.a.appearance['class_id']=3
        with patch('nso_server.mixins.combat.secrets.randbelow',return_value=0):
            self.assertEqual(self.s.authoritative_pvp_damage(self.a,self.b),1)

    async def test_critical_and_flat_mitigation_order(self):
        self.a.appearance.update(damage=1000,fatal=1000)
        self.a.appearance['combat']['damage2']=1000
        with patch('nso_server.mixins.combat.secrets.randbelow',return_value=0):
            self.assertEqual(self.s.authoritative_pvp_damage(self.a,self.b),79)
        self.assertTrue(self.a.last_pvp_fatal)
        self.b.appearance['damage_down']=80
        with patch('nso_server.mixins.combat.secrets.randbelow',return_value=0):
            self.assertEqual(self.s.authoritative_pvp_damage(self.a,self.b),0)

    async def test_source_rectangle_cooldown_and_mana_rejection(self):
        with patch('nso_server.mixins.combat.secrets.randbelow',return_value=0):
            self.assertGreaterEqual(await self.s.validate_pvp_attack(self.a,self.b,1),0)
        self.assertEqual(await self.s.validate_pvp_attack(self.a,self.b,1),-1)
        self.a.pvp_ready_at[1]=time.monotonic()-1
        self.b.x=152
        self.assertEqual(await self.s.validate_pvp_attack(self.a,self.b,1),-1)
        self.b.x=120;self.a.appearance['mp']=0
        self.assertEqual(await self.s.validate_pvp_attack(self.a,self.b,1),-1)
        self.assertEqual(self.a.appearance['mp'],0)

    async def test_missing_profile_stun_weapon_and_map_zone_fail_closed(self):
        for mode in ('missing','stunned','weapon','village','zone','dead'):
            a=copy.deepcopy(self.a);b=copy.deepcopy(self.b)
            if mode=='missing':a.appearance.pop('combat')
            elif mode=='stunned':a.appearance['combat']['locked_ms']=60000
            elif mode=='weapon':a.appearance['weapon']=-1
            elif mode=='village':a.map_id=b.map_id=22
            elif mode=='zone':b.zone_id=1
            elif mode=='dead':a.hp=0
            self.assertEqual(await self.s.validate_pvp_attack(a,b,1),-1,mode)

    async def test_revive_server_deadline_and_source_effect_are_misses(self):
        self.b.revive_protected_until=time.monotonic()+5
        with patch('nso_server.mixins.combat.secrets.randbelow',return_value=0):
            self.assertEqual(await self.s.validate_pvp_attack(self.a,self.b,1),0)
        self.assertEqual(self.a.appearance['mp'],999)
        self.b.revive_protected_until=time.monotonic()-1
        self.b.appearance['combat']['miss_ms']=5000
        self.assertEqual(self.s.authoritative_pvp_damage(self.a,self.b),0)

    async def test_combat_metadata_bounds_and_binary_integer_clock(self):
        p=native_profile();p['combat'].update(dx=999999,cooldown=-1,mana=999999,_at=-999,miss_ms=9999999)
        c=self.s.clean_appearance(p)['combat']
        self.assertEqual((c['dx'],c['cooldown'],c['mana'],c['miss_ms']),(2000,0,65535,600000))
        self.assertIsInstance(c['_at'],int);self.assertGreater(c['_at'],0)

    async def test_source_stealth_is_one_shot_and_lang_co_exception(self):
        self.a.appearance['combat']['stealth_ms']=5000
        self.a.appearance['combat']['invisible_ms']=5000
        self.a.map_id=self.b.map_id=138
        with patch('nso_server.mixins.combat.secrets.randbelow',return_value=0):
            self.assertGreaterEqual(await self.s.validate_pvp_attack(self.a,self.b,1),0)
        self.assertTrue(self.a.last_pvp_fatal)
        self.assertEqual(self.a.appearance['combat']['stealth_ms'],0)
        self.assertEqual(self.a.appearance['combat']['invisible_ms'],0)

async def export_wire(fixtures,profile=None,fan_profile=None):
    fixtures=Path(fixtures);fixtures.mkdir(parents=True,exist_ok=True)
    profile=profile or native_profile();fan_profile=fan_profile or dict(native_profile(),class_id=6)
    state=RoomServer();server=await asyncio.start_server(state.handle,'127.0.0.1',0)
    port=server.sockets[0].getsockname()[1];writers=[];trace=[]
    async def event(reader,label,kind):
        for _ in range(24):
            e=await asyncio.wait_for(receive(reader),5);trace.append(dict(label=label,event=e))
            if e['type']==kind:return e
        raise AssertionError(kind)
    async def connect(name):
        r,w=await asyncio.open_connection('127.0.0.1',port);writers.append(w)
        welcome=await event(r,name,'welcome');await send(w,dict(cmd='hello',name=name));await event(r,name,'hello_ok');return r,w,welcome['player_id']
    try:
        ar,aw,aid=await connect('EighthOwner');br,bw,bid=await connect('EighthGuest')
        await send(aw,dict(cmd='create',room='EIGHTHCOMBAT'));await event(ar,'create','room_joined')
        await send(bw,dict(cmd='join',room='EIGHTHCOMBAT'));await event(br,'join','room_joined');await event(ar,'other','player_join')
        for w,r,name in ((aw,br,'EighthOwner'),(bw,ar,'EighthGuest')):
            await send(w,dict(profile,cmd='appearance',name=name));await event(r,'appearance','player_appearance')
        for w,r,x,hp in ((aw,br,100,1000),(bw,ar,120,1000)):
            await send(w,dict(cmd='state',map=2,zone=0,x=x,y=100,hp=hp));await event(r,'state','player_state')
        a=state.rooms['EIGHTHCOMBAT'].players[aid];b=state.rooms['EIGHTHCOMBAT'].players[bid]
        await send(aw,dict(cmd='interaction',kind='cuu_sat',target_actor=b.actor_id));await event(ar,'pk','cuu_sat_start');await event(br,'pk','cuu_sat_start')
        await send(aw,dict(cmd='interaction',kind='pvp_attack',target_actor=b.actor_id,damage=2000000000,skill=1));hit=await event(br,'pk-hit','pvp_hit');await event(ar,'pk-hit','pvp_hit')
        assert 0<=abs(hit['damage'])<=180 and hit['hp']==1000-abs(hit['damage']),hit
        assert a.appearance['mp']==999
        print('EIGHTH_TCP_SOURCE_PVP damage='+str(hit['damage'])+' hp='+str(hit['hp'])+' mp='+str(a.appearance['mp']),flush=True)
        await send(aw,dict(cmd='interaction',kind='pvp_attack',target_actor=b.actor_id,damage=2000000000,skill=1));error=await event(ar,'cooldown','error');assert error['code']=='skill_cooldown',error
        await send(aw,dict(cmd='interaction',kind='cuu_sat_clear',target_actor=0));await event(ar,'clear','cuu_sat_end');await event(br,'clear','cuu_sat_end')
        await send(bw,dict(cmd='state',map=2,zone=0,x=200,y=100,hp=b.hp));await event(ar,'far','player_state')
        await send(aw,dict(cmd='interaction',kind='duel_invite',target_actor=b.actor_id));await event(br,'invite','duel_invite')
        await send(bw,dict(cmd='interaction',kind='duel_accept',target_actor=a.actor_id));await event(ar,'duel','duel_start');await event(br,'duel','duel_start')
        await send(aw,dict(cmd='interaction',kind='duel_attack',target_actor=b.actor_id,damage=2000000000,skill=1));error=await event(ar,'range','error');assert error['code']=='target_too_far',error
        print('EIGHTH_TCP_RANGE_REJECT dx=100 learned=51',flush=True)
        await send(aw,dict(cmd='interaction',kind='duel_cancel',target_actor=b.actor_id));await event(ar,'cancel','duel_end');await event(br,'cancel','duel_end')
        await send(aw,dict(fan_profile,cmd='appearance',name='EighthOwner'));await event(br,'fan','player_appearance')
        await send(bw,dict(cmd='state',map=2,zone=0,x=120,y=100,hp=0));await event(ar,'dead','player_state')
        await send(aw,dict(cmd='interaction',kind='player_revive',target_actor=b.actor_id,skill=49,data='200,35'));revive=await event(br,'revive','player_revived');await event(ar,'revive','player_revived')
        await send(aw,dict(cmd='interaction',kind='mob_attack',target_actor=b.actor_id,data='0,120,100',damage=100,skill=1));protected=await event(br,'protected','mob_hit');await event(ar,'protected','mob_hit')
        assert protected['hp']==1000 and protected['damage']==-1 and protected['amount']==0,protected
        print('EIGHTH_TCP_PROTECTED hp=1000 shown=-1 drain=0',flush=True)
        await asyncio.sleep(5.05)
        await send(aw,dict(cmd='interaction',kind='mob_attack',target_actor=b.actor_id,data='0,120,100',damage=100,skill=1));expired=await event(br,'expired','mob_hit');await event(ar,'expired','mob_hit')
        assert expired['hp']==980 and expired['damage']==100 and expired['amount']==20,expired
        print('EIGHTH_TCP_EXPIRED hp=980 shown=100 drain=20',flush=True)
        for name,e in [('revive',revive),('mob-protected',protected),('mob-expired',expired),('pvp-hit',hit)]:
            (fixtures/(name+'.json')).write_text(json.dumps(e,ensure_ascii=False,separators=(',',':')),encoding='utf8')
        (fixtures/'native-input.json').write_text(json.dumps(profile,ensure_ascii=False,indent=2),encoding='utf8')
    finally:
        (fixtures/'trace.json').write_text(json.dumps(trace,ensure_ascii=False,indent=2),encoding='utf8')
        for w in writers:w.close()
        for w in writers:
            try:await w.wait_closed()
            except (ConnectionError,OSError):pass
        server.close();await server.wait_closed()

class EighthCombatWireTest(unittest.IsolatedAsyncioTestCase):
    async def test_source_scale_range_and_actual_revive_mob_deadline_tcp(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:await export_wire(Path(directory))

if __name__=='__main__':
    if '--fixtures' in sys.argv:
        p=argparse.ArgumentParser();p.add_argument('--fixtures',type=Path,required=True);p.add_argument('--appearance',type=Path);p.add_argument('--fan-appearance',type=Path);a=p.parse_args()
        asyncio.run(export_wire(a.fixtures,json.loads(a.appearance.read_text(encoding='utf8')) if a.appearance else None,json.loads(a.fan_appearance.read_text(encoding='utf8')) if a.fan_appearance else None))
    else:unittest.main()
