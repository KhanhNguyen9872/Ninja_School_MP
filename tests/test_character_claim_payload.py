"""Character ownership uses Java's sendInteraction data field on both transports."""
import asyncio
import unittest

from test_server import receive, send
from nso_server.service import RoomServer


class CharacterClaimPayloadTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state=RoomServer()
        self.server=await asyncio.start_server(self.state.handle, '127.0.0.1', 0)
        self.port=self.server.sockets[0].getsockname()[1]

    async def connect(self, name):
        reader,writer=await asyncio.open_connection('127.0.0.1', self.port)
        welcome=await receive(reader)
        await send(writer, {'cmd':'hello', 'name':name})
        self.assertEqual((await receive(reader))['type'], 'hello_ok')
        return reader,writer,welcome['player_id']

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        await asyncio.sleep(.03)
        self.state._save_executor.shutdown(wait=True)

    async def test_java_data_claim_join_duplicate_release_and_legacy_text(self):
        owner_reader, owner_writer, _ = await self.connect('owner')
        guest_reader, guest_writer, _ = await self.connect('guest')
        try:
            await send(owner_writer, {'cmd':'create', 'room':'PAYLOAD'})
            self.assertEqual((await receive(owner_reader))['type'], 'room_joined')
            await send(guest_writer, {'cmd':'join', 'room':'PAYLOAD'})
            self.assertEqual((await receive(guest_reader))['type'], 'room_joined')
            self.assertEqual((await receive(owner_reader))['type'], 'player_join')
            identity='0123456789abcdef'
            payload={'cmd':'interaction', 'kind':'character_claim', 'data':identity,
                     'target_actor':0, 'damage':0, 'skill':0}
            await send(owner_writer, payload)
            ack=await receive(owner_reader)
            self.assertEqual((ack['type'], ack['data']), ('character_claimed', identity))
            await send(guest_writer, payload)
            self.assertEqual((await receive(guest_reader))['code'], 'character_online')
            await send(owner_writer, dict(payload, kind='character_release'))
            await send(owner_writer, {'cmd':'ping'})
            self.assertEqual((await receive(owner_reader))['type'], 'pong')
            await send(guest_writer, payload)
            self.assertEqual((await receive(guest_reader))['type'], 'character_claimed')
            # data owns the contract even if an older text field is present.
            await send(guest_writer, dict(payload, data='', text=identity))
            self.assertEqual((await receive(guest_reader))['code'], 'character_id_invalid')
            await send(guest_writer, {'cmd':'interaction', 'kind':'character_claim',
                                     'text':identity, 'target_actor':0, 'damage':0, 'skill':0})
            self.assertEqual((await receive(guest_reader))['type'], 'character_claimed')
        finally:
            owner_writer.close();guest_writer.close()
            await owner_writer.wait_closed();await guest_writer.wait_closed()


if __name__ == '__main__':
    unittest.main()
