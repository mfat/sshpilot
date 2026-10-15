"""Loopback-TCP daemon endpoint used where AF_UNIX is unavailable (Windows)."""

from __future__ import annotations

import socket
import threading

from sshpilot.api.transport import endpoint


def test_preamble_gate_accepts_split_valid_preamble_and_returns_rest():
    token = endpoint.new_token()
    gate = endpoint.PreambleGate(token)
    preamble = endpoint.auth_preamble(token)
    assert gate.feed(preamble[:5]) is None
    assert gate.feed(preamble[5:] + b"frame") == b"frame"


def test_preamble_gate_rejects_wrong_token_early():
    gate = endpoint.PreambleGate(endpoint.new_token())
    assert gate.feed(b"GET / HTTP/1.1\r\n") is False


def test_preamble_gate_rejects_wrong_token_of_full_length():
    gate = endpoint.PreambleGate(endpoint.new_token())
    assert gate.feed(endpoint.auth_preamble(endpoint.new_token())) is False


def test_publish_and_read_round_trip(tmp_path):
    path = tmp_path / "run" / "sshpilotd.json"
    published = endpoint.TcpEndpoint(port=4242, token=endpoint.new_token(), pid=99)
    identity = endpoint.publish_tcp_endpoint(path, published)
    assert identity == (path.stat().st_dev, path.stat().st_ino)
    assert endpoint.read_tcp_endpoint(path) == published


def test_read_rejects_malformed_endpoint(tmp_path):
    path = tmp_path / "sshpilotd.json"
    path.write_text('{"port": 0, "token": "short"}', encoding="utf-8")
    assert endpoint.read_tcp_endpoint(path) is None
    assert endpoint.read_tcp_endpoint(tmp_path / "missing.json") is None


def test_connect_tcp_sends_preamble_first():
    listener, port = endpoint.create_tcp_listener()
    listener.listen()
    token = endpoint.new_token()
    received = {}

    def serve():
        conn, _ = listener.accept()
        with conn:
            received["ok"] = endpoint.read_preamble_blocking(conn, token)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        client = endpoint.connect_tcp(endpoint.TcpEndpoint(port=port, token=token), timeout=5)
        client.close()
        thread.join(5)
    finally:
        listener.close()
    assert received["ok"] is True


def test_read_preamble_blocking_rejects_bad_token():
    left, right = socket.socketpair()
    try:
        left.sendall(endpoint.auth_preamble(endpoint.new_token()))
        assert endpoint.read_preamble_blocking(right, endpoint.new_token(), timeout=2) is False
    finally:
        left.close()
        right.close()
