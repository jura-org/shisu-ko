"""The server holds its port alone: no SO_REUSEADDR on Windows, where the option shares a port.

On Windows a socket with SO_REUSEADDR can bind an address and port that another socket with the
option already listens on, and the two then split the connections. http.server sets it, so
another listener on 127.0.0.1:8790 (WSL's relay for a Docker container, a second Python server)
could sit beside the native server unnoticed. OwnPortHTTPServer leaves the option off there, as
socket.create_server() does, and keeps it on POSIX, where it only lets a restart bind over the
old server's connections in TIME_WAIT.
"""
from __future__ import annotations

import inspect
import os
import socket
from types import SimpleNamespace

import pytest

from _serverlib import load_server

server = load_server()


class OsAs:
    """server.py's `os` with another os.name; everything else is the real module's."""

    def __init__(self, name):
        self.name = name

    def __getattr__(self, attr):
        return getattr(os, attr)


def bound(monkeypatch, platform):
    monkeypatch.setattr(server, "os", OsAs(platform))
    return server.OwnPortHTTPServer(("127.0.0.1", 0), server.Handler)


def reuse_flag(httpd) -> bool:
    return bool(httpd.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR))


def test_the_server_sets_no_reuse_flag_on_windows(monkeypatch):
    httpd = bound(monkeypatch, "nt")
    try:
        assert reuse_flag(httpd) is False
        assert httpd.allow_reuse_address is False
    finally:
        httpd.server_close()


def test_the_server_keeps_the_reuse_flag_on_posix(monkeypatch):
    httpd = bound(monkeypatch, "posix")
    try:
        assert reuse_flag(httpd) is True, "a restart must bind over the old server's TIME_WAIT connections"
    finally:
        httpd.server_close()


def test_main_listens_through_the_own_port_server():
    main_body = inspect.getsource(server.main)
    assert "OwnPortHTTPServer((args.host, args.port), Handler)" in main_body
    assert "ThreadingHTTPServer(" not in main_body


def reusing_listener(port: int = 0) -> socket.socket:
    """A listener as WSL's relay or a second Python server opens one: with SO_REUSEADDR."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(5)
    return s


@pytest.mark.skipif(os.name != "nt", reason="the port sharing exists on Windows only")
def test_on_windows_the_server_refuses_a_port_another_listener_holds():
    other = reusing_listener()
    try:
        with pytest.raises(OSError):
            server.OwnPortHTTPServer(("127.0.0.1", other.getsockname()[1]), server.Handler)
    finally:
        other.close()


@pytest.mark.skipif(os.name != "nt", reason="the port sharing exists on Windows only")
def test_on_windows_nothing_can_join_the_server_s_port():
    httpd = server.OwnPortHTTPServer(("127.0.0.1", 0), server.Handler)
    try:
        with pytest.raises(OSError):
            reusing_listener(httpd.server_address[1]).close()
    finally:
        httpd.server_close()


@pytest.mark.skipif(os.name != "nt", reason="the port sharing exists on Windows only")
def test_on_windows_a_restart_binds_over_the_old_server_s_time_wait():
    # run.cmd starts the server again 5 s after a crash; the old one closed its connections first,
    # so they sit in TIME_WAIT on its side. Windows binds over them without SO_REUSEADDR.
    server.Handler.app = SimpleNamespace(health=lambda: {"ok": True})
    first = server.OwnPortHTTPServer(("127.0.0.1", 0), server.Handler)
    port = first.server_address[1]
    clients = [socket.create_connection(("127.0.0.1", port)) for _ in range(3)]
    accepted = [first.socket.accept()[0] for _ in clients]
    for conn in accepted:
        conn.close()  # the server closes first: TIME_WAIT on its side
    for client in clients:
        client.recv(1)
        client.close()
    first.server_close()
    try:
        second = server.OwnPortHTTPServer(("127.0.0.1", port), server.Handler)
        second.server_close()
    finally:
        server.Handler.app = None
