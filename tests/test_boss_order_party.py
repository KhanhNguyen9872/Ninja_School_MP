"""Rikudou boss order party admission (NSO_FINAL Char.orderTaskBoss)."""
import unittest
from nso_server.models import Player, Room
from nso_server.service import RoomServer
from nso_server.protocol import _decode_value, EVENT_TYPES


class Writer:
    def __init__(self): self.events = []
    def write(self, b):
        p, _ = _decode_value(b[8:]); p['type'] = EVENT_TYPES[b[3] - 64]; self.events.append(p)
    async def drain(self): pass


class BossOrderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = RoomServer(quiet=True)
        self.room = Room('BOSS', owner_id='a')
        self.p = {i: Player(i, i.upper(), Writer(), actor_id=10001 + n, hp=100)
                  for n, i in enumerate(('a', 'b', 'c'))}
        self.room.players = self.p
        self.room.parties['a'] = ['a', 'b', 'c']
        self.p['c'].zone_id = 9   # different zone: skipped, as Char.getCharsInZone

    async def asyncTearDown(self): self.server._save_executor.shutdown(wait=True)

    def types(self, who): return [e['type'] for e in self.p[who].writer.events]

    async def call(self, who, kind, data=''):
        return await self.server.interaction(self.room, self.p[who], {'kind': kind, 'data': data})

    async def test_member_cannot_start(self):
        await self.call('b', 'task_boss_order')
        self.assertEqual(self.p['b'].writer.events[-1]['code'], 'not_leader')

    async def test_assign_after_all_ack_ok(self):
        await self.call('a', 'task_boss_order')
        probe = self.p['b'].writer.events[-1]
        self.assertEqual(probe['type'], 'task_probe')
        self.assertNotIn('task_probe', self.types('c'))
        self.assertNotIn('task_assign', self.types('a'))
        await self.call('b', 'task_boss_ack', probe['data'] + ',0')
        self.assertIn('task_assign', self.types('a'))
        self.assertIn('task_assign', self.types('b'))
        self.assertNotIn('task_assign', self.types('c'))

    async def test_member_refusal_denies_leader_and_assigns_nobody(self):
        await self.call('a', 'task_boss_order')
        probe = self.p['b'].writer.events[-1]
        await self.call('b', 'task_boss_ack', probe['data'] + ',2')
        self.assertEqual(self.p['a'].writer.events[-1]['code'], 'low_level')
        self.assertNotIn('task_assign', self.types('a'))
        self.assertEqual(self.room.task_probes, {})

    async def test_solo_leader_is_assigned_immediately(self):
        self.room.parties.clear()
        await self.call('a', 'task_boss_order')
        self.assertIn('task_assign', self.types('a'))

    async def test_dead_member_is_skipped(self):
        self.p['b'].hp = 0
        await self.call('a', 'task_boss_order')
        self.assertIn('task_assign', self.types('a'))
        self.assertNotIn('task_probe', self.types('b'))


if __name__ == '__main__':
    unittest.main()
