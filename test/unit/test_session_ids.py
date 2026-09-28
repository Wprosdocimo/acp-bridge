"""Default session id scoping (src/session_ids.py) and its use by /runs and /jobs.

Regression: callers that sent no session_id all landed on one session per
agent. Two callers in different worktrees then shared one agent process; the
pool rebound its cwd and respawned it mid-run, crashing both.
"""

import os
import sys
import types
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from acp_sdk.models import Message, MessagePart

from src import agents
from src.session_ids import default_session_id


def test_no_cwd_keeps_historical_per_agent_id():
    assert default_session_id("kiro") == str(uuid.uuid5(uuid.NAMESPACE_DNS, "kiro"))
    assert default_session_id("kiro") == "fba032b1-cc32-53f1-9fe7-adc2c163a406"


def test_different_cwds_get_different_sessions():
    a = default_session_id("kiro", "/repos/worktree-a")
    b = default_session_id("kiro", "/repos/worktree-b")
    assert a != b
    assert default_session_id("kiro") not in (a, b)


def test_equivalent_cwd_spellings_share_a_session():
    base = default_session_id("kiro", "/repos/app")
    assert default_session_id("kiro", "/repos/app/") == base
    assert default_session_id("kiro", "/repos/./app") == base


def test_same_cwd_different_agents_differ():
    assert default_session_id("kiro", "/repos/app") != default_session_id("claude", "/repos/app")


class _Ctx:
    def __init__(self, headers):
        self.request = types.SimpleNamespace(headers=headers, query_params={})
        self.session = None


async def _session_used(monkeypatch, headers):
    seen = []

    async def fake_call(**kwargs):
        seen.append(kwargs["session_id"])
        yield MessagePart(content="ok", content_type="text/plain")

    monkeypatch.setattr(agents, "_call_acp_agent_internal", fake_call)
    handler = agents.make_acp_agent_handler("kiro", pool=None)
    msg = Message(parts=[MessagePart(content="hi", content_type="text/plain")])
    async for _ in handler([msg], _Ctx(headers)):
        pass
    return seen[0]


@pytest.mark.asyncio
async def test_runs_without_session_is_scoped_by_cwd_header(monkeypatch):
    a = await _session_used(monkeypatch, {"x-acp-cwd": "/repos/worktree-a"})
    b = await _session_used(monkeypatch, {"x-acp-cwd": "/repos/worktree-b"})
    assert a == default_session_id("kiro", "/repos/worktree-a")
    assert a != b


@pytest.mark.asyncio
async def test_runs_without_session_or_cwd_uses_per_agent_id(monkeypatch):
    assert await _session_used(monkeypatch, {}) == default_session_id("kiro")
