"""Contribution escrow decisions, durable ACK confirmations and activity points."""
import asyncio
import json
import time
import unittest
from pathlib import Path
from test_seventh_settlement import Fixture
from test_server import receive, send
from nso_server.service import RoomServer


class EighthSettlementTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = Fixture()
        self.clan = self.f.clan()
        self.clan['coin'] = 1900000000

    async def asyncTearDown(self):
        self.f.close()

    async def test_contribution_idempotent_exact_cap_then_rejected(self):
        f = self.f
        await f.call('clan_contribute', data='cc-first,100000000')
        first = f.last('self', 'activity_state')
        self.assertEqual((first['category'], self.clan['coin']), (1, 2000000000))
        await f.call('clan_contribute', data='cc-first,100000000')
        self.assertEqual(f.last('self', 'activity_state')['delivery_id'], first['delivery_id'])
        self.assertEqual(self.clan['coin'], 2000000000)
        await f.call('clan_contribute', data='cc-second,100000000')
        rejected = f.last('self', 'activity_state')
        self.assertEqual((rejected['category'], rejected['amount'], self.clan['coin']),
                         (0, 100000000, 2000000000))
        self.assertIn(rejected['delivery_id'], f.room.pending_deliveries['self'])

    async def test_two_members_cannot_saturate_and_lose_partial_contribution(self):
        f = self.f
        await asyncio.gather(f.call('clan_contribute', data='cc-self,100000000'),
                             f.call('clan_contribute', 'peer', data='cc-peer,100000000'))
        decisions = [f.last(i, 'activity_state') for i in ('self', 'peer')]
        self.assertEqual(sorted(e['category'] for e in decisions), [0, 1])
        self.assertEqual(self.clan['coin'], 2000000000)
        self.assertEqual(sum(e['amount'] for e in decisions if e['category']), 100000000)

    async def test_contribution_identity_and_amount_cannot_be_rebound(self):
        f = self.f
        await f.call('clan_contribute', data='cc-bound,1000')
        await f.call('clan_contribute', 'peer', data='cc-bound,1000')
        self.assertEqual(f.last('peer', 'error')['code'], 'clan_amount_invalid')
        await f.call('clan_contribute', data='cc-bound,2000')
        self.assertEqual(f.last('self', 'error')['code'], 'clan_amount_invalid')
        self.assertEqual(self.clan['coin'], 1900001000)

    async def test_legacy_over_cap_never_silently_clamped(self):
        await self.f.call('clan_contribute', data='100000000')
        await self.f.call('clan_contribute', data='1000')
        self.assertEqual(self.f.last('self', 'error')['code'], 'clan_fund_limit')
        self.assertEqual(self.clan['coin'], 2000000000)

    async def test_ack_confirmation_is_idempotent_after_obligation_removed(self):
        f = self.f
        ident = await f.server.durable_delivery(f.room, 'self',
                {'type': 'mob_reward', 'amount': 1000}, 'eighth-yen')
        for _ in range(2):
            await f.call('delivery_ack', data=ident + '|Y:' + ident)
            confirmation = f.last('self', 'activity_state')
            self.assertEqual((confirmation['phase'], confirmation['data']),
                             ('receipt_ack', 'Y:' + ident))
        self.assertNotIn('self', f.room.pending_deliveries)

    async def test_activity_points_ack_commits_shared_state_only_once(self):
        f = self.f
        before = self.clan['exp']
        ident = await f.server.durable_delivery(f.room, 'peer',
                {'type': 'activity_reward', 'data': 'Fujuka|p,20'}, 'eighth-points')
        self.assertEqual(self.clan['exp'], before)
        await f.call('delivery_ack', 'peer', data=ident + '|A:' + ident)
        self.assertEqual((self.clan['exp'], self.clan['members']['peer']['points']), (before + 20, 20))
        await f.call('delivery_ack', 'peer', data=ident + '|A:' + ident)
        self.assertEqual((self.clan['exp'], self.clan['members']['peer']['points']), (before + 20, 20))

    async def test_no_clan_activity_points_no_phantom_membership(self):
        f = self.f
        self.clan['members'].pop('outside')
        ident = await f.server.durable_delivery(f.room, 'outside',
                {'type': 'activity_reward', 'data': 'Fujuka|p,20'}, 'eighth-noclan')
        before = self.clan['exp']
        await f.call('delivery_ack', 'outside', data=ident + '|A:' + ident)
        self.assertEqual(self.clan['exp'], before)
        self.assertNotIn('outside', self.clan['members'])

    async def test_contribution_decision_is_present_in_snapshot(self):
        f = self.f
        await f.call('clan_contribute', data='cc-snapshot,1000')
        snap = f.server._snapshot_state()
        self.assertIn('cc-snapshot', snap['worlds']['FIX7']['clan_contributions'])


class EighthSettlementWireTest(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_contribution_decision_and_confirmation_retry(self):
        state = RoomServer(quiet=True)
        listener = await asyncio.start_server(state.handle, '127.0.0.1', 0)
        reader, writer = await asyncio.open_connection('127.0.0.1', listener.sockets[0].getsockname()[1])
        try:
            welcome = await receive(reader)
            await send(writer, {'cmd': 'hello', 'name': 'Eighth'})
            await receive(reader)
            await send(writer, {'cmd': 'create', 'room': 'EIGHTH'})
            await receive(reader)
            room = state.rooms['EIGHTH']
            player = room.players[welcome['player_id']]
            clan = state.ensure_clan(room, player, 'WireEighth')
            clan.update(level=10, exp=500, coin=1900000000)
            async def event(kind):
                while True:
                    value = await asyncio.wait_for(receive(reader), 3)
                    if value['type'] == kind:
                        return value
            for ident, accepted in (('cc-wire-first', 1), ('cc-wire-second', 0)):
                await send(writer, {'cmd': 'interaction', 'kind': 'clan_contribute',
                                    'data': ident + ',100000000'})
                decision = await event('activity_state')
                self.assertEqual(decision['category'], accepted)
                for _ in range(2):
                    await send(writer, {'cmd': 'interaction', 'kind': 'delivery_ack',
                                        'data': decision['delivery_id'] + '|CC:' + ident})
                    self.assertEqual((await event('activity_state'))['phase'], 'receipt_ack')
            self.assertEqual(clan['coin'], 2000000000)
            self.assertFalse(room.pending_deliveries)
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(.05)
            listener.close()
            await listener.wait_closed()
            state._save_executor.shutdown(wait=True)


async def export_fixtures(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    f = Fixture()
    try:
        clan = f.clan()
        clan['coin'] = 1900000000
        def save(name, value):
            (path / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')), encoding='utf8')
        save('clan-before', f.server.clan_payload(f.room, clan))
        await f.call('clan_contribute', data='cc-native-accept,1000')
        save('contribution-accepted', f.last('self', 'activity_state'))
        clan['coin'] = 2000000000
        await f.call('clan_contribute', data='cc-native-reject,1000')
        save('contribution-rejected', f.last('self', 'activity_state'))
    finally:
        f.close()
    print('EIGHTH_SETTLEMENT_FIXTURES_EXPORTED')


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == '--fixtures':
        asyncio.run(export_fixtures(sys.argv[2]))
    else:
        unittest.main()
