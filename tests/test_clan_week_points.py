"""TCP clan-week state, card exclusion, reset and idempotent seal debit."""
import asyncio
import time
import unittest
from unittest.mock import patch, AsyncMock
from test_server import send, receive as _receive, RoomServer


async def receive(reader):
    return await asyncio.wait_for(_receive(reader), 5)


class ClanWeekPointsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = RoomServer(quiet=True)
        self.server = await asyncio.start_server(self.state.handle, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.writers = []

    async def asyncTearDown(self):
        for w in self.writers:
            w.close()
        await asyncio.gather(*(w.wait_closed() for w in self.writers), return_exceptions=True)
        self.server.close()
        await self.server.wait_closed()

    async def connect(self, name):
        r, w = await asyncio.open_connection('127.0.0.1', self.port)
        self.writers.append(w)
        pid = (await receive(r))['player_id']
        await send(w, {'cmd': 'hello', 'name': name})
        await receive(r)
        return r, w, pid

    async def setup_clan(self):
        a = await self.connect('WeekOwner')
        await send(a[1], {'cmd': 'create', 'room': 'WEEK'})
        await receive(a[0])
        b = await self.connect('WeekPeer')
        await send(b[1], {'cmd': 'join', 'room': 'WEEK'})
        await receive(b[0]); await receive(a[0])
        room = self.state.rooms['WEEK']
        clan = self.state.ensure_clan(room, room.players[a[2]], 'WeekClan')
        peer = room.players[b[2]]
        clan['members'][b[2]] = {'name': peer.name, 'class_id': 1, 'level': 100, 'role': 0, 'points': 0}
        peer.clan_name = clan['name']
        return a, b, room, clan

    @staticmethod
    def rows(event):
        return {row.split(':')[0]: row.split(':') for row in event['data'].split(';')}

    async def test_tcp_points_cards_and_both_member_rows(self):
        a, b, room, clan = await self.setup_clan()
        for amount, weekly in ((1200, 1200), (200, 0)):
            await send(a[1], {'cmd': 'interaction', 'kind': 'clan_points',
                             'data': f'{amount},{weekly}'})
            owner, peer = await receive(a[0]), await receive(b[0])
            self.assertEqual(owner['type'], 'clan_state')
            self.assertEqual(self.rows(peer)[a[2]][7], '1200')
        self.assertEqual(self.rows(peer)[a[2]][5], '1400')
        self.assertEqual(self.rows(peer)[b[2]][7], '0')

    async def test_guest_seal_debit_replay_and_insufficient(self):
        a, b, room, clan = await self.setup_clan()
        week = self.state.refresh_clan_weeks(clan)
        clan['members'][b[2]]['week_points'] = 2000
        message = {'cmd': 'interaction', 'kind': 'clan_week_spend',
                   'data': f'1000,ws-seal-one,{week},WeekClan'}
        delivery = None
        for _ in range(2):
            await send(b[1], message)
            receipt = await receive(b[0])
            self.assertEqual((receipt['type'], receipt['phase'], receipt['category']),
                             ('clan_item_delivery', 'clan_week_seal', 1))
            self.assertEqual((receipt['data'], receipt['target_id']), ('870,1,1,0,0,-1,', b[2]))
            if delivery is not None: self.assertEqual(receipt['delivery_id'], delivery)
            delivery = receipt['delivery_id']
            await receive(a[0]); event = await receive(b[0])
            self.assertEqual(self.rows(event)[b[2]][7], '1000')
        self.assertIn(delivery, room.pending_deliveries[b[2]])
        await send(b[1], dict(message, data=f'1000,ws-seal-two,{week},WeekClan'))
        await receive(b[0])
        await receive(a[0]); event = await receive(b[0])
        self.assertEqual(self.rows(event)[b[2]][7], '0')
        await send(b[1], dict(message, data=f'1000,ws-seal-three,{week},WeekClan'))
        denied = await receive(b[0]); await receive(a[0]); await receive(b[0])
        self.assertEqual((denied['category'], denied['data']), (0, ''))
        self.assertEqual(clan['members'][b[2]]['week_points'], 0)

    async def test_stale_week_wrong_clan_and_receipt_after_rollover(self):
        a, b, room, clan = await self.setup_clan()
        week = self.state.refresh_clan_weeks(clan)
        clan['members'][b[2]]['week_points'] = 2000
        for tx, epoch, name in [('ws-stale', week - 1, 'WeekClan'), ('ws-wrong', week, 'OtherClan')]:
            await send(b[1], {'cmd': 'interaction', 'kind': 'clan_week_spend',
                             'data': f'1000,{tx},{epoch},{name}'})
            denial = await receive(b[0]); await receive(a[0]); await receive(b[0])
            self.assertEqual(denial['category'], 0)
            self.assertEqual(clan['members'][b[2]]['week_points'], 2000)
        request = {'cmd': 'interaction', 'kind': 'clan_week_spend',
                   'data': f'1000,ws-paid-before-rollover,{week},WeekClan'}
        await send(b[1], request)
        receipt = await receive(b[0]); await receive(a[0]); await receive(b[0])
        # A durable paid item is not erased when its points epoch rolls over.
        with patch('nso_server.mixins.social.time.time', return_value=time.time() + 7 * 86400):
            self.state.refresh_clan_weeks(clan)
            await send(b[1], request)
            replay = await receive(b[0]); await receive(a[0]); await receive(b[0])
        self.assertEqual(replay['delivery_id'], receipt['delivery_id'])
        self.assertEqual(clan['members'][b[2]]['week_points'], 0)
        self.assertIn(receipt['delivery_id'], room.pending_deliveries[b[2]])
        # A different member cannot claim another recipient's request id.
        self.assertFalse(await self.state.handle_clan_interaction(
            room, room.players[a[2]], None, request, {}, 'clan_week_spend'))
        clan['members'].pop(b[2]); room.players[b[2]].clan_name = ''
        await send(b[1], request)
        left_clan_replay = await receive(b[0])
        self.assertEqual(left_clan_replay['delivery_id'], receipt['delivery_id'])
        self.assertEqual(left_clan_replay['target_id'], b[2])

    async def test_storage_failure_before_send_retries_same_paid_decision(self):
        a, b, room, clan = await self.setup_clan()
        week = self.state.refresh_clan_weeks(clan)
        clan['members'][b[2]]['week_points'] = 2000
        request = {'data': f'1000,ws-save-retry,{week},WeekClan'}
        with patch.object(self.state, 'flush_state', new=AsyncMock(side_effect=OSError('injected disk failure'))), \
                patch('nso_server.mixins.interaction_clan.send_json', new=AsyncMock()) as send_stub:
            with self.assertRaises(OSError):
                await self.state.handle_clan_interaction(
                    room, room.players[b[2]], None, request, {}, 'clan_week_spend')
            send_stub.assert_not_awaited()
        decision = dict(room.clan_contributions['ws-save-retry'])
        self.assertEqual(clan['members'][b[2]]['week_points'], 1000)
        await self.state.handle_clan_interaction(
            room, room.players[b[2]], None, request, {}, 'clan_week_spend')
        replay = await receive(b[0]); await receive(a[0]); await receive(b[0])
        self.assertEqual(replay['delivery_id'], decision['delivery_id'])
        self.assertEqual(clan['members'][b[2]]['week_points'], 1000)

    async def test_rollover_keeps_total_and_clears_only_week(self):
        a, b, room, clan = await self.setup_clan()
        member = clan['members'][a[2]]
        week = self.state.refresh_clan_weeks(clan)
        member.update(points=5000, week_points=2000, week_spends={'old': 1000})
        # Seven days always crosses the same Monday-based GMT+7 epoch.
        future = time.time() + 7 * 86400
        with patch('nso_server.mixins.social.time.time', return_value=future):
            event = self.state.clan_payload(room, clan)
        self.assertEqual(member['points'], 5000)
        self.assertEqual(member['week_points'], 0)
        self.assertEqual(member['week_spends'], {})
        self.assertEqual(int(self.rows(event)[a[2]][8]), week + 1)

    async def test_legacy_delta_cannot_mint_weekly_points(self):
        a, b, room, clan = await self.setup_clan()
        await send(a[1], {'cmd': 'interaction', 'kind': 'clan_points', 'data': '200'})
        await receive(a[0]); event = await receive(b[0])
        row = self.rows(event)[a[2]]
        self.assertEqual((row[5], row[7]), ('200', '0'))


if __name__ == '__main__':
    unittest.main()
