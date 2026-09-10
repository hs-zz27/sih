"""H4 - the out-of-process observer must see what netguard cannot reach.

netguard patches this interpreter's socket module; it cannot see a raw
connection made by a subprocess. netmonitor polls `lsof` against our whole
process tree instead, so these tests hold a real connection open and check
that the *external* poll (not a mock) reports it.
"""

from __future__ import annotations

import socket
import time

import pytest

from src.io import audit, netmonitor

# The observer shells out to a platform tool. Where that tool is absent the
# monitor degrades to an error string by design, so these tests skip rather than
# fail - a red suite on a teammate's machine hides real regressions.
pytestmark = pytest.mark.skipif(
    netmonitor.poll_once().error is not None,
    reason=f"no out-of-process observer on this platform: {netmonitor.poll_once().error}",
)


@pytest.fixture(autouse=True)
def isolated_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "log_path", lambda: tmp_path / "audit.jsonl")


def _reachable(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=3).close()
        return True
    except OSError:
        return False


def test_poll_reports_no_error_when_lsof_is_available():
    result = netmonitor.poll_once()
    assert result.error is None


def test_external_connection_is_observed():
    if not _reachable("1.1.1.1", 443):
        pytest.skip("no network reachable from this sandbox - cannot exercise the observer")

    sock = socket.create_connection(("1.1.1.1", 443), timeout=4)
    try:
        time.sleep(0.3)  # let the OS-level connection table settle
        result = netmonitor.poll_once()
        hosts = {conn.remote_host for conn in result.external}
        assert "1.1.1.1" in hosts
    finally:
        sock.close()


def test_loopback_connection_is_not_flagged_external():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port), timeout=4)
    try:
        time.sleep(0.3)
        result = netmonitor.poll_once()
        assert not any(conn.remote_port == port for conn in result.external)
    finally:
        client.close()
        server.close()


def test_background_monitor_logs_external_connections():
    if not _reachable("1.1.1.1", 443):
        pytest.skip("no network reachable from this sandbox - cannot exercise the observer")

    monitor = netmonitor.NetworkMonitor(interval_s=0.3)
    monitor.start()
    try:
        time.sleep(0.4)
        sock = socket.create_connection(("1.1.1.1", 443), timeout=4)
        try:
            time.sleep(1.0)
        finally:
            sock.close()
        time.sleep(0.4)
    finally:
        monitor.stop()

    events = audit.read_events()
    external = [e for e in events if e.external and e.actor == "netmonitor"]
    assert external, "the background poller must record what it observed"
    assert "1.1.1.1" in external[0].summary


def test_monitor_deduplicates_repeated_observations():
    """The same (pid, host, port) seen on every poll must be logged once."""
    if not _reachable("1.1.1.1", 443):
        pytest.skip("no network reachable from this sandbox - cannot exercise the observer")

    monitor = netmonitor.NetworkMonitor(interval_s=0.2)
    monitor.start()
    try:
        sock = socket.create_connection(("1.1.1.1", 443), timeout=4)
        try:
            time.sleep(1.2)  # several poll cycles over the same connection
        finally:
            sock.close()
    finally:
        monitor.stop()

    events = [e for e in audit.read_events() if e.actor == "netmonitor"]
    assert len(events) == 1
