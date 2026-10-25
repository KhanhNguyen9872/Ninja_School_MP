"""Deputy (role 3) clan authority: kick, level-up and fresh territory open (NSO_FINAL Char)."""
import unittest
from nso_server.models import Player, Room
from nso_server.service import RoomServer


class Writer:
    def write(self, frame): pass
    async def drain(self): pass


class ClanDeputyAuthorityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = RoomServer(quiet=True)
        self.room = Room('DEPUTY', owner_id='a')
        self.a = Player('a', 'A', Writer(), actor_id=10001)
        self.b = Player('b', 'B', Writer(), actor_id=10002)
        self.c = Player('c', 'C', Writer(), actor_id=10003)
        self.room.players = {'a': self.a, 'b': self.b, 'c': self.c}
        self.clan = self.state.ensure_clan(self.room, self.a, 'Deputy')
        self.clan.update(level=1, coin=10000000, exp=100000000)
        self.clan['members']['b'] = {'role': 3, 'name': 'B'}
        self.clan['members']['c'] = {'role': 0, 'name': 'C'}
        self.c.clan_name = 'Deputy'

    async def asyncTearDown(self):
        self.state._save_executor.shutdown(wait=True)

    async def test_deputy_kicks_ordinary_member_but_not_leader(self):
        await self.state.interaction(self.room, self.b, {'kind': 'clan_kick', 'target_actor': 10001})
        self.assertIn('a', self.clan['members'])
        await self.state.interaction(self.room, self.b, {'kind': 'clan_kick', 'target_actor': 10003})
        self.assertNotIn('c', self.clan['members'])
        self.assertEqual(self.clan['coin'], 10000000 - 10000)

    async def test_ordinary_member_cannot_kick(self):
        self.clan['members']['c']['role'] = 0
        await self.state.interaction(self.room, self.c, {'kind': 'clan_kick', 'target_actor': 10002})
        self.assertIn('b', self.clan['members'])

    async def test_deputy_levels_clan_up(self):
        await self.state.interaction(self.room, self.b, {'kind': 'clan_upgrade'})
        self.assertEqual(self.clan['level'], 2)

    async def test_ordinary_member_cannot_level_up(self):
        await self.state.interaction(self.room, self.c, {'kind': 'clan_upgrade'})
        self.assertEqual(self.clan['level'], 1)

    async def test_deputy_opens_fresh_territory_without_room_ownership(self):
        await self.state.interaction(self.room, self.b, {'kind': 'clan_territory_open'})
        self.assertTrue(self.clan['territory'].get('active'))


if __name__ == '__main__':
    unittest.main()
