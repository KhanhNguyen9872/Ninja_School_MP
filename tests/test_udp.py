import asyncio
import struct
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1]))

from nso_server.protocol import (EVENT_OPCODES, REQUEST_OPCODES, _decode_value,
                                 _encode_value)
from nso_server.service import RoomServer
from nso_server.udp import start_udp_server


def request(payload):
    body = dict(payload)
    command = body.pop("cmd")
    opcode = next(code for code, name in REQUEST_OPCODES.items() if name == command)
    encoded = _encode_value(body)
    return b"NS\x02" + bytes((opcode,)) + struct.pack(">I", len(encoded)) + encoded


def event(data):
    if len(data) < 8 or data[:3] != b"NS\x02":
        raise AssertionError("invalid UDP frame")
    size = struct.unpack(">I", data[4:8])[0]
    payload, consumed = _decode_value(data[8:])
    if size != len(data) - 8 or consumed != size:
        raise AssertionError("invalid UDP payload")
    payload["type"] = next(name for name, opcode in EVENT_OPCODES.items()
                           if opcode == data[3])
    return payload


class ClientProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None
        self.received = asyncio.Queue()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        self.received.put_nowait(event(data))

    def send(self, payload):
        self.transport.sendto(request(payload))

    async def receive(self):
        return await asyncio.wait_for(self.received.get(), 2.0)


async def tcp_send(writer, payload):
    writer.write(request(payload))
    await writer.drain()


async def tcp_receive(reader):
    header = await reader.readexactly(8)
    size = struct.unpack(">I", header[4:8])[0]
    return event(header + await reader.readexactly(size))


class UdpTransportTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = RoomServer()
        self.tcp = await asyncio.start_server(self.state.handle, "127.0.0.1", 0)
        self.port = self.tcp.sockets[0].getsockname()[1]
        self.udp, self.udp_server = await start_udp_server(
            self.state, "127.0.0.1", self.port)

    async def asyncTearDown(self):
        self.tcp.close()
        await self.tcp.wait_closed()
        self.udp.close()
        await asyncio.sleep(0)

    async def test_udp_and_tcp_clients_share_one_room(self):
        loop = asyncio.get_running_loop()
        udp_transport, udp = await loop.create_datagram_endpoint(
            ClientProtocol, remote_addr=("127.0.0.1", self.port))
        udp.send({"cmd": "hello", "name": "UdpOwner"})
        self.assertEqual((await udp.receive())["type"], "welcome")
        self.assertEqual((await udp.receive())["type"], "hello_ok")
        udp.send({"cmd": "create", "room": "MIXED"})
        self.assertEqual((await udp.receive())["type"], "room_joined")

        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.assertEqual((await tcp_receive(reader))["type"], "welcome")
        await tcp_send(writer, {"cmd": "hello", "name": "TcpGuest"})
        self.assertEqual((await tcp_receive(reader))["type"], "hello_ok")
        await tcp_send(writer, {"cmd": "join", "room": "MIXED"})
        self.assertEqual((await tcp_receive(reader))["type"], "room_joined")
        self.assertEqual((await udp.receive())["type"], "player_join")

        udp.send({"cmd": "chat", "text": "hello across transports"})
        chat = await tcp_receive(reader)
        self.assertEqual((chat["type"], chat["text"]),
                         ("chat", "hello across transports"))

        udp.send({"cmd": "disconnect"})
        udp_transport.close()
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
