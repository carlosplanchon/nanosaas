"""
The limit on the size of request bodies, MAX_BODY_SIZE: FastAPI reads and
parses the whole body of a route before it checks the API key or the
session, so without it anyone could fill the API's memory.
"""
import socket
import time
from urllib.parse import urlsplit

import pytest

from tests.helpers import client_for

pytestmark = pytest.mark.anyio

# The default MAX_BODY_SIZE: 1 MiB.
LIMIT = 1024 * 1024
TOO_LARGE = b'{"detail":"The request\'s body is larger than the limit of 1048576 bytes."}'
BODY = b'{"x": 6, "y": 3}'
JSON = {"Content-Type": "application/json"}


def raw_request(server, head, chunks=(), timeout=5):
    """
    Send a request's head and some of its body, without credentials, over a
    connection of its own, and read the answer until the server closes the
    connection, within timeout seconds. Returns the answer, and whether the
    server closed the connection: a server waiting for the rest of the body
    answers nothing.
    """
    url = urlsplit(server.url)
    answer = b""
    with socket.create_connection((url.hostname, url.port), timeout=timeout) as connection:
        connection.sendall(head)
        for chunk in chunks:
            connection.sendall(chunk)
        deadline = time.monotonic() + timeout
        try:
            while data := connection.recv(65536):
                answer += data
                connection.settimeout(max(deadline - time.monotonic(), 0.01))
        except TimeoutError:
            return answer, False
    return answer, True


def chunk(data):
    """A chunk of a body sent with Transfer-Encoding: chunked."""
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


async def chunked(data):
    """A body that httpx sends with Transfer-Encoding: chunked, without its length."""
    yield data


def test_a_body_larger_than_the_limit_gets_413_before_it_arrives(server):
    # A gigabyte, of which nothing is sent: the API doesn't wait for it.
    answer, closed = raw_request(server, (
        b"POST /api/task/divide HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n"
        b"Content-Length: 1000000000\r\n\r\n"
    ))
    assert answer.startswith(b"HTTP/1.1 413 ")
    assert answer.endswith(b"\r\n\r\n" + TOO_LARGE)
    assert b"\r\nconnection: close\r\n" in answer.lower()
    # It closes the connection with the answer, so the server doesn't go on reading the body.
    assert closed
    # With the headers of every answer.
    assert b"\r\nx-content-type-options: nosniff\r\n" in answer.lower()


def test_a_body_without_its_length_gets_413_once_it_passes_the_limit(server):
    # One byte more than the limit, in chunks, without the last one, which would end the body:
    # the API answers once the limit is passed, without waiting for the rest.
    answer, closed = raw_request(
        server,
        b"POST /api/task/divide HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n",
        [chunk(b" " * 65536) for _ in range(LIMIT // 65536)] + [chunk(b" ")],
    )
    assert answer.startswith(b"HTTP/1.1 413 ")
    assert answer.endswith(b"\r\n\r\n" + TOO_LARGE)
    assert closed


async def test_a_body_of_the_limit_gets_through(start_server):
    small = start_server("small-body", max_body_size=1000)
    async with client_for(small) as client:
        # Padded with spaces, which JSON allows, with its length and without it.
        for content in (BODY.ljust(1000), chunked(BODY.ljust(1000))):
            r = await client.post("/api/task/divide", content=content, headers=JSON)
            # Through the limit, to the check of the API key.
            assert (r.status_code, r.json()) == (401, {"detail": "Invalid API key"})

        for content in (BODY.ljust(1001), chunked(BODY.ljust(1001))):
            r = await client.post("/api/task/divide", content=content, headers=JSON)
            assert r.status_code == 413
            assert r.json() == {"detail": "The request's body is larger than the limit of 1000 bytes."}
