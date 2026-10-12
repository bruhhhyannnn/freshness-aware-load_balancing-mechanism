import asyncio

from lbstudy.protocol import HEADER, U32, Link, Msg, encode, read_frame, set_nodelay


def test_frame_layout():
    frame = encode(Msg.STATUS_REPLY, 7, U32.pack(42))
    assert len(frame) == HEADER.size + 4 == 17
    assert HEADER.unpack(frame[:HEADER.size]) == (Msg.STATUS_REPLY, 7, 4)


def test_read_frame_round_trip():
    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(encode(Msg.REQUEST, 1) + encode(Msg.STATS_REPLY, 2, b'{"a": 1}'))
        reader.feed_eof()
        return [await read_frame(reader), await read_frame(reader)]

    assert asyncio.run(go()) == [(Msg.REQUEST, 1, b""), (Msg.STATS_REPLY, 2, b'{"a": 1}')]


def test_link_matches_out_of_order_replies():
    """Replies sent in reverse order must still reach the right caller."""

    async def echo_reversed(reader, writer):
        set_nodelay(writer)
        frames = [await read_frame(reader) for _ in range(3)]
        for _, req_id, payload in reversed(frames):
            writer.write(encode(Msg.RESPONSE, req_id, payload))
        await writer.drain()

    async def go():
        server = await asyncio.start_server(echo_reversed, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        link = await Link.open("127.0.0.1", port)
        replies = await asyncio.gather(*(link.call(Msg.REQUEST, bytes([i])) for i in range(3)))
        await link.close()
        server.close()
        return [payload for _, payload in replies]

    assert asyncio.run(go()) == [b"\x00", b"\x01", b"\x02"]
