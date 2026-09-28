"""Tests for AcpProcessPool.get_or_create bounded wait (v0.40.0)."""

import asyncio
import time

import pytest

from src.acp_client import AcpError, AcpProcessPool, PoolExhaustedError


def make_pool(timeout: float, agents: dict | None = None) -> AcpProcessPool:
    pool = AcpProcessPool(
        agents_config=agents or {"a": {}, "b": {}}, max_processes=2, max_per_agent=1
    )
    pool._acquire_timeout = timeout
    return pool


@pytest.mark.asyncio
async def test_wait_then_succeed(monkeypatch):
    """Pool full on first attempts → waits → slot frees → returns connection."""
    pool = make_pool(timeout=10)
    calls = {"n": 0}

    async def fake(agent, session_id, cwd="", profile=None, resume_session_id=""):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PoolExhaustedError("per-agent limit")
        return f"conn-{agent}"

    monkeypatch.setattr(pool, "_get_or_create_unlocked", fake)
    t0 = time.monotonic()
    conn = await pool.get_or_create("a", "s1")
    dt = time.monotonic() - t0
    assert conn == "conn-a"
    assert calls["n"] == 3
    assert 3.0 < dt < 7.0  # two 2s sleeps between three attempts


@pytest.mark.asyncio
async def test_timeout_raises(monkeypatch):
    """Pool full for longer than acquire_timeout → PoolExhaustedError, bounded."""
    pool = make_pool(timeout=5)

    async def always_full(agent, session_id, cwd="", profile=None, resume_session_id=""):
        raise PoolExhaustedError("global limit")

    monkeypatch.setattr(pool, "_get_or_create_unlocked", always_full)
    t0 = time.monotonic()
    with pytest.raises(PoolExhaustedError):
        await pool.get_or_create("a", "s1")
    dt = time.monotonic() - t0
    assert dt <= 5.5  # never exceeds timeout
    assert dt >= 3.0  # actually waited before giving up


@pytest.mark.asyncio
async def test_zero_timeout_fail_fast(monkeypatch):
    """acquire_timeout=0 → old behavior: single attempt, immediate raise."""
    pool = make_pool(timeout=0)
    calls = {"n": 0}

    async def always_full(agent, session_id, cwd="", profile=None, resume_session_id=""):
        calls["n"] += 1
        raise PoolExhaustedError("global limit")

    monkeypatch.setattr(pool, "_get_or_create_unlocked", always_full)
    t0 = time.monotonic()
    with pytest.raises(PoolExhaustedError):
        await pool.get_or_create("a", "s1")
    assert calls["n"] == 1
    assert time.monotonic() - t0 < 0.5


@pytest.mark.asyncio
async def test_wait_does_not_block_other_agents(monkeypatch):
    """The wait loop must not hold the pool lock — agent b acquires
    while agent a is waiting for a slot."""
    pool = make_pool(timeout=10)

    async def unlocked(agent, session_id, cwd="", profile=None, resume_session_id=""):
        if agent == "a":
            raise PoolExhaustedError("per-agent limit for a")
        return f"conn-{agent}"

    monkeypatch.setattr(pool, "_get_or_create_unlocked", unlocked)

    waiter = asyncio.create_task(pool.get_or_create("a", "s1"))
    await asyncio.sleep(0.3)  # waiter is now sleeping between attempts
    t0 = time.monotonic()
    conn = await pool.get_or_create("b", "s2")
    dt = time.monotonic() - t0
    assert conn == "conn-b"
    assert dt < 0.5, f"agent b blocked for {dt:.1f}s while agent a waits"
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter


@pytest.mark.asyncio
async def test_other_errors_propagate_immediately(monkeypatch):
    """Non-pool errors (e.g. AcpError) are not retried by the wait loop."""
    from src.acp_client import AcpError

    pool = make_pool(timeout=10)
    calls = {"n": 0}

    async def broken(agent, session_id, cwd="", profile=None, resume_session_id=""):
        calls["n"] += 1
        raise AcpError("agent not found: a")

    monkeypatch.setattr(pool, "_get_or_create_unlocked", broken)
    t0 = time.monotonic()
    with pytest.raises(AcpError):
        await pool.get_or_create("a", "s1")
    assert calls["n"] == 1
    assert time.monotonic() - t0 < 0.5


class FakeConn:
    """Minimal stand-in for AcpConnection: only what the cwd check touches."""

    def __init__(self, cwd: str = "", busy: bool = False):
        self.cwd = cwd
        self.alive = True
        self._busy = busy
        self.killed = False

    async def kill(self):
        self.killed = True
        self.alive = False


def make_cwd_pool(monkeypatch) -> AcpProcessPool:
    pool = make_pool(timeout=0, agents={"a": {"command": "agent-a"}, "b": {"command": "agent-b"}})
    monkeypatch.setattr(pool, "_save_pids", lambda: None)
    return pool


@pytest.mark.asyncio
async def test_live_connection_reused_when_cwd_matches(monkeypatch):
    """Same cwd (or none requested) → warm process is handed back untouched."""
    pool = make_cwd_pool(monkeypatch)
    conn = FakeConn(cwd="/repo")
    pool._connections[("a", "s1")] = conn

    assert await pool.get_or_create("a", "s1", cwd="/repo") is conn
    assert await pool.get_or_create("a", "s1") is conn
    assert not conn.killed


@pytest.mark.asyncio
async def test_different_cwd_respawns_in_requested_dir(monkeypatch):
    """A live process bound to another dir is retired, not silently reused."""
    pool = make_cwd_pool(monkeypatch)
    old = FakeConn(cwd="/tmp")
    pool._connections[("a", "s1")] = old
    spawned = {}

    async def fake_spawn(agent, session_id, cfg, **kwargs):
        spawned["cwd"] = kwargs.get("cwd_override")
        spawned["is_rebuild"] = kwargs.get("is_rebuild")
        return FakeConn(cwd=kwargs.get("cwd_override", ""))

    monkeypatch.setattr(pool, "_spawn", fake_spawn)

    conn = await pool.get_or_create("a", "s1", cwd="/repo")
    assert old.killed
    assert conn is not old
    assert conn.cwd == "/repo"
    assert spawned["cwd"] == "/repo"
    assert spawned["is_rebuild"] is False
    assert pool._connections[("a", "s1")] is conn


@pytest.mark.asyncio
async def test_busy_connection_refuses_cwd_rebind(monkeypatch):
    """Mid-flight process must not be killed under the running request."""
    pool = make_cwd_pool(monkeypatch)
    busy = FakeConn(cwd="/tmp", busy=True)
    pool._connections[("a", "s1")] = busy

    with pytest.raises(AcpError, match="busy"):
        await pool.get_or_create("a", "s1", cwd="/repo")
    assert not busy.killed
    assert pool._connections[("a", "s1")] is busy
