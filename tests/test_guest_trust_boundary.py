"""Guest-supplied mob metadata, party EXP, drops and stale boss ACKs (audit 20261025)."""
import unittest
from nso_server.models import Player, Room
from nso_server.service import RoomServer
from nso_server.protocol import _decode_value, EVENT_TYPES


class Writer:
    def __init__(self): self.events = []
    def write(self, b):
        p, _ = _decode_value(b[8:]); p['type'] = EVENT_TYPES[b[3] - 64]; self.events.append(p)
    async def drain(self): pass


class GuestTrustTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = RoomServer(quiet=True)
        self.room = Room('TRUST', owner_id='a')
        self.p = {i: Player(i, i.upper(), Writer(), actor_id=10001 + n, hp=100, map_id=1)
                  for n, i in enumerate(('a', 'b', 'c'))}
        for pl in self.p.values():
            pl.appearance = {'level': 50, 'class_id': 1}; pl.room_id = 'TRUST'; pl.hello_ok = True
        self.room.players = self.p
        self.server.rooms[self.room.room_id] = self.room
        self.room.parties['a'] = ['a', 'b']

    async def asyncTearDown(self): self.server._save_executor.shutdown(wait=True)

    def types(self, who): return [e['type'] for e in self.p[who].writer.events]

    async def cmd(self, who, message):
        return await self.server.command(self.p[who], message)

    def seed_mob(self, **extra):
        mob = {'mob_id': '7', 'map': 1, 'zone': 0, 'template': 10, 'level_boss': 0,
               'hp': 10, 'max_hp': 10, 'alive': True, 'respawn_at': 0, 'x': 0, 'y': 0}
        mob.update(extra)
        self.room.mobs['1:0:7'] = mob
        return mob

    async def test_guest_cannot_rewrite_projected_mob_metadata(self):
        mob = self.seed_mob()
        await self.cmd('b', {'cmd': 'attack', 'mob_id': '7', 'damage': 10,
                             'drop_template': 999, 'level_boss': 3})
        self.assertEqual((mob['template'], mob['level_boss']), (10, 0))
        kills = [(e['template'], e['level_boss']) for e in self.p['a'].writer.events if e['type'] == 'party_kill']
        self.assertEqual(kills, [(10, 0)])

    async def test_party_kill_carries_server_metadata(self):
        self.seed_mob()
        await self.cmd('a', {'cmd': 'attack', 'mob_id': '7', 'damage': 10})
        self.room.players['b'].writer.events.clear()
        self.seed_mob()
        await self.cmd('a', {'cmd': 'attack', 'mob_id': '7', 'damage': 10})
        kill = next(e for e in self.p['b'].writer.events if e['type'] == 'party_kill')
        self.assertEqual(kill['template'], 10)

    async def test_party_exp_requires_a_damaging_hit_on_a_projected_mob(self):
        await self.cmd('b', {'cmd': 'attack', 'mob_id': 'never', 'damage': 0, 'hp': 10,
                             'drop_template': 1, 'party_exp': 2147483647})
        self.assertNotIn('party_reward', self.types('a'))

    async def test_party_exp_is_bounded_per_hit(self):
        self.seed_mob(max_hp=100, hp=100)
        await self.cmd('b', {'cmd': 'attack', 'mob_id': '7', 'damage': 10, 'party_exp': 2147483647})
        rewards = [e for e in self.p['a'].writer.events if e['type'] == 'party_reward']
        self.assertEqual([e['amount'] for e in rewards], [100000])

    async def test_guest_thrown_drop_is_not_claimable_by_others(self):
        self.room.parties.clear()
        for pl in self.p.values(): pl.x = pl.y = 0
        await self.cmd('b', {'cmd': 'drop', 'item_id': 5, 'template': 60000, 'quantity': 9, 'x': 0, 'y': 0})
        drop = self.room.drops['b-5']; drop['protected_until'] = 0
        await self.cmd('c', {'cmd': 'pickup', 'drop_id': 'b-5'})
        self.assertIn('b-5', self.room.drops)
        await self.cmd('b', {'cmd': 'pickup', 'drop_id': 'b-5'})
        self.assertNotIn('b-5', self.room.drops)

    async def test_stale_boss_ack_after_leader_left_assigns_nothing(self):
        await self.server.interaction(self.room, self.p['a'], {'kind': 'task_boss_order'})
        probe = next(e for e in self.p['b'].writer.events if e['type'] == 'task_probe')
        self.room.players.pop('a')
        await self.server.purge_member_state(self.room, 'a')
        await self.server.interaction(self.room, self.p['b'],
                                     {'kind': 'task_boss_ack', 'data': probe['data'] + ',0'})
        self.assertNotIn('task_assign', self.types('b'))
        self.assertEqual(self.room.task_probes, {})


if __name__ == '__main__':
    unittest.main()
