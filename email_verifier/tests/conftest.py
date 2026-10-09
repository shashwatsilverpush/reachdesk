"""Shared fixtures: network guard and mock SMTP servers.

All tests run with ZERO network access — the autouse ``no_network``
fixture monkeypatches ``socket.socket.connect`` so that only 127.0.0.1
is reachable.  Accidental DNS or SMTP calls to the internet fail loudly.

Mock SMTP servers are threaded socket servers implementing the minimal
SMTP subset needed for testing (EHLO, MAIL FROM, RCPT TO, QUIT).
"""

from __future__ import annotations

import socket
import threading
from typing import Any, Callable, Generator

import pytest

from email_verifier.dnsx import clear_mx_cache


# ---------------------------------------------------------------------------
#  Network guard
# ---------------------------------------------------------------------------

_real_connect = socket.socket.connect


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block all network access except 127.0.0.1."""

    def guarded_connect(self: socket.socket, address: Any) -> None:
        if isinstance(address, tuple) and len(address) >= 2:
            host = address[0]
            if host == "127.0.0.1" or host == "localhost":
                return _real_connect(self, address)
        raise OSError("network disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    """Clear MX cache between tests."""
    clear_mx_cache()


# ---------------------------------------------------------------------------
#  Threaded mock SMTP server
# ---------------------------------------------------------------------------

class MockSMTPServer:
    """Minimal threaded SMTP server for testing.

    Accepts connections on 127.0.0.1 with an ephemeral port and
    dispatches RCPT TO commands to a handler callback.
    """

    def __init__(self, rcpt_handler: Callable[[str], str]) -> None:
        self._handler = rcpt_handler
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(1.0)
        self.hostname = "127.0.0.1"
        self.port: int = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
                threading.Thread(
                    target=self._handle_client,
                    args=(conn,),
                    daemon=True,
                ).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle_client(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(10.0)
            conn.sendall(b"220 mock ESMTP ready\r\n")
            buf = b""
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    cmd = line.decode(errors="replace").strip()
                    upper = cmd.upper()
                    if upper.startswith("EHLO") or upper.startswith("HELO"):
                        conn.sendall(b"250 Hello\r\n")
                    elif upper.startswith("MAIL FROM"):
                        conn.sendall(b"250 OK\r\n")
                    elif upper.startswith("RCPT TO"):
                        addr = cmd.split(":", 1)[1].strip().strip("<>")
                        resp = self._handler(addr)
                        conn.sendall(resp.encode() + b"\r\n")
                    elif upper.startswith("QUIT"):
                        conn.sendall(b"221 Bye\r\n")
                        return
                    elif upper.startswith("DATA"):
                        conn.sendall(b"354 Go ahead\r\n")
                    elif upper.startswith("RSET"):
                        conn.sendall(b"250 OK\r\n")
                    else:
                        conn.sendall(b"500 Unknown command\r\n")
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def stop(self) -> None:
        """Shut down the server."""
        self._stop.set()
        try:
            self._sock.close()
        except Exception:
            pass
        self._thread.join(timeout=3)


# ---------------------------------------------------------------------------
#  RCPT TO handler functions
# ---------------------------------------------------------------------------

def _truthful_rcpt(address: str) -> str:
    """Accept locals starting with 'exists', reject everything else."""
    local = address.split("@")[0]
    if local.startswith("exists"):
        return "250 OK"
    return "550 5.1.1 User unknown"


def _catchall_rcpt(address: str) -> str:
    """Accept everything."""
    return "250 OK"


class _GreylistRcpt:
    """First RCPT per local part returns 450; subsequent ones return 250."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def __call__(self, address: str) -> str:
        local = address.split("@")[0]
        with self._lock:
            if local not in self._seen:
                self._seen.add(local)
                return "450 4.7.1 Greylisted"
        return "250 OK"


def _full_rcpt(address: str) -> str:
    """Always return 552 Mailbox full."""
    return "552 5.2.2 Mailbox full"


class _AppleLikeRcpt:
    """Accept the first RCPT across all connections, then 421 thereafter."""

    def __init__(self) -> None:
        self._count = 0
        self._lock = threading.Lock()

    def __call__(self, address: str) -> str:
        with self._lock:
            self._count += 1
            if self._count == 1:
                return "250 OK"
        return "421 Service not available"


# ---------------------------------------------------------------------------
#  Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def truthful_server() -> Generator[tuple[str, int], None, None]:
    """SMTP server that reveals mailbox existence truthfully."""
    srv = MockSMTPServer(_truthful_rcpt)
    yield srv.hostname, srv.port
    srv.stop()


@pytest.fixture
def catchall_server() -> Generator[tuple[str, int], None, None]:
    """SMTP server that accepts every RCPT TO."""
    srv = MockSMTPServer(_catchall_rcpt)
    yield srv.hostname, srv.port
    srv.stop()


@pytest.fixture
def greylist_server() -> Generator[tuple[str, int], None, None]:
    """SMTP server that greylists on first attempt per local part."""
    srv = MockSMTPServer(_GreylistRcpt())
    yield srv.hostname, srv.port
    srv.stop()


@pytest.fixture
def full_server() -> Generator[tuple[str, int], None, None]:
    """SMTP server that always returns 552 Mailbox full."""
    srv = MockSMTPServer(_full_rcpt)
    yield srv.hostname, srv.port
    srv.stop()


@pytest.fixture
def apple_like() -> Generator[tuple[str, int], None, None]:
    """SMTP server that accepts first RCPT then 421s subsequent ones."""
    srv = MockSMTPServer(_AppleLikeRcpt())
    yield srv.hostname, srv.port
    srv.stop()
