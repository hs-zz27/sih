"""H4 - the egress guard must block outward and permit loopback.

Both halves matter. A guard that blocks everything would also break the local
inference server, and a guard that permits everything proves nothing.
"""

from __future__ import annotations

import socket

import pytest

from src.io import audit, netguard
from src.io.netguard import ExternalCallBlocked


@pytest.fixture
def guard(tmp_path, monkeypatch):
    """Install the guard against a throwaway audit log."""
    monkeypatch.setattr(audit, "log_path", lambda: tmp_path / "audit.jsonl")
    netguard.install()
    yield
    netguard.uninstall()


@pytest.mark.parametrize(
    "host",
    ["8.8.8.8", "api.openai.com", "api.anthropic.com", "huggingface.co", "1.1.1.1"],
)
def test_external_connect_is_blocked(guard, host):
    with pytest.raises(ExternalCallBlocked):
        socket.create_connection((host, 443), timeout=1)


@pytest.mark.parametrize("host", ["example.com", "google.com", "8.8.8.8"])
def test_external_dns_resolution_is_blocked(guard, host):
    """Resolution is egress too - and it is what a stray cloud SDK does first."""
    with pytest.raises(ExternalCallBlocked):
        socket.getaddrinfo(host, 443)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_is_permitted(guard, host):
    """The local inference server must stay reachable."""
    assert netguard.is_local(host)
    # Resolving loopback must not raise; connecting is refused by the OS
    # (nothing listening) rather than by the guard.
    socket.getaddrinfo(host, 11434)


def test_blocked_attempt_is_written_to_the_audit_log(guard):
    with pytest.raises(ExternalCallBlocked):
        socket.create_connection(("api.openai.com", 443), timeout=1)

    events = audit.read_events()
    external = [event for event in events if event.external]
    assert external, "a blocked call must leave an audit record"
    assert "api.openai.com" in external[-1].summary
    assert external[-1].actor == "netguard"


def test_guard_is_idempotent_and_reversible(guard):
    netguard.install()  # second call must not double-wrap
    assert netguard.is_installed()
    netguard.uninstall()
    assert not netguard.is_installed()
    # Restored layer resolves loopback without the guard in place.
    socket.getaddrinfo("127.0.0.1", 80)
    netguard.install()


def test_hostnames_are_not_resolved_to_decide_locality(guard):
    """A DNS answer is attacker-influenced; only exact allow-list matches pass."""
    assert not netguard.is_local("localhost.evil.com")
    assert not netguard.is_local("127.0.0.1.evil.com")
