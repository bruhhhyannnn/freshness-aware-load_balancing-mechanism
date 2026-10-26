"""Wire protocol shared by the load generator, load balancer and backend servers.

All components talk over persistent TCP connections using small binary
frames. Every frame starts with a fixed 13-byte header:

    offset  size  field
    ------  ----  -----------------------------------------------
    0       1     message type (see ``Msg``)
    1       8     request id, unsigned 64-bit, big-endian
    9       4     payload length in bytes, unsigned 32-bit, big-endian
    13      n     payload

The request id lets many requests be in flight on one connection and lets
replies come back in any order (multiplexing, as in HTTP/2 or gRPC). The
testbed therefore needs one connection per hop rather than one per
outstanding request: a herding burst that queues a thousand requests on one
server cannot exhaust ephemeral ports or add TCP handshakes to the
latencies being measured.
"""

from __future__ import annotations

import asyncio
import itertools
import socket
import struct
from enum import IntEnum

HEADER = struct.Struct("!BQI")
U16 = struct.Struct("!H")
U32 = struct.Struct("!I")
# STATUS_REPLY payload: queue length, report time (unix s), requests completed
# so far, seconds spent processing so far.
STATUS = struct.Struct("!IdQd")


class Msg(IntEnum):
    REQUEST = 1        # client -> balancer -> server: process one job; payload is
                       # empty or one byte naming the task class (lbstudy.tasks)
    RESPONSE = 2       # server -> balancer -> client; payload: server id (u16)
    STATUS = 3         # balancer -> server: report your current queue length
    STATUS_REPLY = 4   # payload: ``STATUS`` (queue, time, completed, busy)
    STATS = 5          # experiment runner -> server/balancer: report counters
    STATS_REPLY = 6    # payload: UTF-8 JSON
    RESET = 7          # experiment runner -> server/balancer: window starts now
    RESET_REPLY = 8
    ERROR = 9          # balancer -> client: request could not be forwarded
    CONTROL = 10       # experiment runner -> server: scenario event (JSON payload)
    CONTROL_REPLY = 11


def encode(kind: Msg, req_id: int, payload: bytes = b"") -> bytes:
    return HEADER.pack(kind, req_id, len(payload)) + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    kind, req_id, size = HEADER.unpack(await reader.readexactly(HEADER.size))
    payload = await reader.readexactly(size) if size else b""
    return kind, req_id, payload


def set_nodelay(writer: asyncio.StreamWriter) -> None:
    """Disable Nagle's algorithm so each small frame leaves immediately."""
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass


class Link:
    """Client end of a multiplexed connection.

    ``send()`` writes a frame and returns a future that resolves to
    ``(reply_type, payload)`` when the reply carrying the same id arrives.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._loop = asyncio.get_running_loop()
        self._reader_task = self._loop.create_task(self._read_replies())

    @classmethod
    async def open(cls, host: str, port: int) -> "Link":
        reader, writer = await asyncio.open_connection(host, port)
        set_nodelay(writer)
        return cls(reader, writer)

    def send(self, kind: Msg, payload: bytes = b"") -> asyncio.Future:
        req_id = next(self._ids)
        reply = self._loop.create_future()
        self._pending[req_id] = reply
        self._writer.write(encode(kind, req_id, payload))
        return reply

    async def call(self, kind: Msg, payload: bytes = b"") -> tuple[int, bytes]:
        return await self.send(kind, payload)

    async def _read_replies(self) -> None:
        try:
            while True:
                kind, req_id, payload = await read_frame(self._reader)
                reply = self._pending.pop(req_id, None)
                if reply is not None and not reply.done():
                    reply.set_result((kind, payload))
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            lost = ConnectionError(f"connection lost: {exc!r}")
            for reply in self._pending.values():
                if not reply.done():
                    reply.set_exception(lost)
            self._pending.clear()

    async def close(self) -> None:
        self._reader_task.cancel()
        for reply in self._pending.values():
            reply.cancel()
        self._pending.clear()
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass
