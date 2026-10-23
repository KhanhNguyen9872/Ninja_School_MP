"""Guest mob target lifecycle, visibility and source radial authority."""
import argparse,asyncio,copy,json,sys,tempfile,time,unittest
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path(sys.argv[sys.argv.index('--mp-root')+1]).resolve() if '--mp-root' in sys.argv else Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'));sys.path.insert(0,str(ROOT))
from server import RoomServer,Room,EVENT_OPCODES,_decode_value
from nso_server.models import Player
from test_server import send,receive

def profile():
    return dict(name='NativeGuest',class_id=1,level=100,weapon=12,max_hp=1000,mp=1000,
                max_mp=2000,damage=3010,damage_down=0,res_fire=0,res_ice=0,res_wind=0,
                combat=dict(v=1,damage2=2709,skill=1,attack_type=1,dx=51,dy=18,
                            mana=1,cooldown=500,invisible_ms=60000,stealth_ms=60000))

class Writer:
    def __init__(self):self.frames=[]
    def write(self,b):self.frames.append(b)
    async def drain(self):pass
    def event(self):
        frame=self.frames[-1];payload,_=_decode_value(frame[8:]);payload['type']=next(k for k,v in EVENT_OPCODES.items() if v==frame[3]);return payload

class NinthCombatDomainTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s=RoomServer(quiet=True);self.a=Player('owner','Owner',Writer(),actor_id=1,map_id=2,x=500,y=100,hp=1000);self.b=Player('guest','Guest',Writer(),actor_id=2,map_id=2,x=110,y=100,hp=1000);self.r=Room('R',owner_id='owner');self.r.players={'owner':self.a,'guest':self.b};self.b.appearance=self.s.clean_appearance(profile())
    async def hit(self,data='0,100,100,0',caster=None):
        return await self.s.handle_gameplay_interaction(self.r,caster or self.a,self.b,dict(data=data,damage=100,skill=1),{},'mob_attack')
    async def test_source_invisibility_rejects_before_hp_mutation(self):
        self.assertFalse(await self.hit());self.assertEqual(self.b.hp,1000);self.assertEqual(self.a.writer.event()['code'],'mob_target_invisible')
    async def test_source_radius_ordinary_and_boss(self):
        self.b.appearance['combat']['invisible_ms']=0
        for dx,dy,boss,accepted in [(300,0,0,True),(212,213,0,True),(301,0,0,False),(500,0,0,False),(500,0,1,True),(600,0,1,True),(601,0,1,False)]:
            self.b.x=100+dx;self.b.y=100+dy;self.b.hp=1000
            self.assertEqual(await self.hit('0,100,100,'+str(boss)),accepted,(dx,dy,boss))
            self.assertEqual(self.b.hp,980 if accepted else 1000)
    async def test_protection_is_not_invisibility_and_owner_death_is_not_world_death(self):
        self.b.appearance['combat'].update(invisible_ms=0,stealth_ms=0,miss_ms=5000);self.a.hp=0
        self.assertTrue(await self.hit());self.assertEqual((self.b.hp,self.b.writer.event()['damage']),(1000,-1))
    async def test_authority_map_zone_dead_and_bad_flag_stay_rejected(self):
        self.b.appearance['combat']['invisible_ms']=0
        self.assertFalse(await self.hit(caster=self.b));self.b.zone_id=1;self.assertFalse(await self.hit());self.b.zone_id=0;self.b.map_id=3;self.assertFalse(await self.hit());self.b.map_id=2;self.b.hp=0;self.assertFalse(await self.hit());self.b.hp=1000;self.assertFalse(await self.hit('0,100,100,2'));self.assertEqual(self.b.hp,1000)
    async def test_snapshot_projects_elapsed_time_without_mutating_canonical(self):
        c=self.b.appearance['combat'];c.update(invisible_ms=1000,stealth_ms=1000,_at=int(time.monotonic()*1000)-2000)
        snapshot=self.s.player_snapshot(self.b);self.assertEqual(snapshot['appearance']['combat']['invisible_ms'],0);self.assertEqual(c['invisible_ms'],1000);self.assertFalse(self.s.combat_effect(self.b,'invisible_ms'))

async def observe_wire(profile_value,output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True);trace=[];result={};state=RoomServer(quiet=True);server=await asyncio.start_server(state.handle,'127.0.0.1',0);port=server.sockets[0].getsockname()[1];writers=[]
    async def next_event(r,label):
        e=await asyncio.wait_for(receive(r),5);trace.append(dict(label=label,event=e));return e
    async def expect(r,label,kind):
        for _ in range(16):
            e=await next_event(r,label)
            if e['type']==kind:return e
        raise RuntimeError(kind)
    async def connect(name):
        r,w=await asyncio.open_connection('127.0.0.1',port);writers.append(w);welcome=await expect(r,name,'welcome');await send(w,dict(cmd='hello',name=name));await expect(r,name,'hello_ok');return r,w,welcome['player_id']
    try:
        ar,aw,aid=await connect('Owner');br,bw,bid=await connect('Guest');await send(aw,dict(cmd='create',room='NINTHCOMBAT'));await expect(ar,'create','room_joined');await send(bw,dict(cmd='join',room='NINTHCOMBAT'));await expect(br,'join','room_joined');await expect(ar,'other','player_join')
        await send(bw,dict(profile_value,cmd='appearance',name='Guest'));await expect(ar,'guest','player_appearance')
        guest_state=None
        for w,r,x in ((aw,br,500),(bw,ar,110)):
            await send(w,dict(cmd='state',map=2,zone=0,x=x,y=100,hp=1000));e=await expect(r,'state','player_state')
            if w is bw:guest_state=e
        guest=state.rooms['NINTHCOMBAT'].players[bid]
        async def attack(label,data):
            await send(aw,dict(cmd='interaction',kind='mob_attack',target_actor=guest.actor_id,data=data,damage=100,skill=1));e=await next_event(ar,label)
            if e['type']=='mob_hit':await expect(br,label,'mob_hit')
            result[label]=e['type']+(':'+str(e['hp']) if e['type']=='mob_hit' else ':'+e.get('code',''));return e
        stealth=await attack('STEALTH_EVENT','0,100,100,0')
        await asyncio.sleep(.08);await send(bw,dict(cmd='snapshot'));snapshot=await expect(br,'snapshot','snapshot');row=next(x for x in snapshot['players'] if x['player_id']==bid);remaining=row['appearance']['combat']['invisible_ms'];result['SNAPSHOT_COUNTS_DOWN']=remaining<int(profile_value['combat']['invisible_ms'])
        clear=copy.deepcopy(profile_value);clear['combat'].update(invisible_ms=0,stealth_ms=0);await send(bw,dict(clear,cmd='appearance',name='Guest'));clear_event=await expect(ar,'clear','player_appearance')
        await send(bw,dict(cmd='state',map=2,zone=0,x=600,y=100,hp=1000));await expect(ar,'boss-position','player_state')
        await attack('NORMAL_OUTER_EVENT','0,100,100,0');boss=await attack('BOSS_OUTER_EVENT','0,100,100,1')
        await send(bw,dict(cmd='state',map=2,zone=0,x=100,y=350,hp=1000));await expect(ar,'radial-position','player_state');radial=await attack('NORMAL_RADIAL_EVENT','0,100,100,0')
        for name,e in [('guest-state',guest_state),('guest-appearance',trace[next(i for i,x in enumerate(trace) if x['label']=='guest')]['event']),('clear-appearance',clear_event),('stealth',stealth),('boss',boss),('radial',radial)]:
            (output/(name+'.json')).write_text(json.dumps(e,ensure_ascii=False,separators=(',',':')),encoding='utf8')
        for key,value in result.items():print(key+'='+str(value).lower() if isinstance(value,bool) else key+'='+value,flush=True)
        return result
    finally:
        (output/'trace.json').write_text(json.dumps(trace,ensure_ascii=False,indent=2),encoding='utf8');(output/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        for w in writers:w.close()
        for w in writers:
            try:await w.wait_closed()
            except (ConnectionError,OSError):pass
        server.close();await server.wait_closed()

def fixed(result):
    assert result['STEALTH_EVENT']=='error:mob_target_invisible',result
    assert result['SNAPSHOT_COUNTS_DOWN'] is True,result
    assert result['NORMAL_OUTER_EVENT']=='error:mob_attack_range',result
    assert result['BOSS_OUTER_EVENT']=='mob_hit:980',result
    assert result['NORMAL_RADIAL_EVENT']=='mob_hit:980',result

class NinthCombatWireTest(unittest.IsolatedAsyncioTestCase):
    async def test_actual_binary_visibility_snapshot_and_source_radius(self):
        with tempfile.TemporaryDirectory() as d:fixed(await observe_wire(profile(),d))

if __name__=='__main__':
    if '--observe' in sys.argv or '--fixtures' in sys.argv:
        p=argparse.ArgumentParser();p.add_argument('--observe',action='store_true');p.add_argument('--fixtures',type=Path,required=True);p.add_argument('--mp-root',type=Path);p.add_argument('--profile',type=Path);a=p.parse_args();value=json.loads(a.profile.read_text(encoding='utf8')) if a.profile else profile();r=asyncio.run(observe_wire(value,a.fixtures))
        if not a.observe:fixed(r)
    else:unittest.main()
