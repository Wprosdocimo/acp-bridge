"""Tests for the optional AWS DevOps Agent safe launcher."""

import sys
import types
from pathlib import Path

import pytest

from src.adapters import aws_devops_launcher
from src.adapters.aws_devops_launcher import (
    ConfigurationError,
    build_server,
    is_explicit_investigation,
    load_settings,
)


def test_launcher_filename_cannot_shadow_upstream_package():
    assert Path(aws_devops_launcher.__file__).stem == "aws_devops_launcher"


def _base_env() -> dict[str, str]:
    return {
        "DEVOPS_AGENT_USER_ID": "operator",
        "DEVOPS_AGENT_SPACE_ID": "space-123",
    }


def test_safe_defaults_require_explicit_investigation():
    assert load_settings(_base_env()) == "explicit"


@pytest.mark.parametrize("missing", ["DEVOPS_AGENT_USER_ID", "DEVOPS_AGENT_SPACE_ID"])
def test_required_identity_and_space_fail_closed(missing):
    env = _base_env()
    env.pop(missing)
    with pytest.raises(ConfigurationError, match=missing):
        load_settings(env)


def test_space_discovery_requires_explicit_opt_out():
    env = {"DEVOPS_AGENT_USER_ID": "operator", "ACP_BRIDGE_AWS_REQUIRE_SPACE_ID": "false"}
    assert load_settings(env) == "explicit"


def test_space_auto_create_requires_two_explicit_switches():
    env = _base_env() | {"DEVOPS_AGENT_AUTO_CREATE_SPACE": "true"}
    with pytest.raises(ConfigurationError, match="auto-creation is blocked"):
        load_settings(env)

    env["ACP_BRIDGE_AWS_ALLOW_SPACE_CREATE"] = "true"
    assert load_settings(env) == "explicit"


def test_invalid_investigation_mode_fails_closed():
    env = _base_env() | {"ACP_BRIDGE_AWS_INVESTIGATION_MODE": "sometimes"}
    with pytest.raises(ConfigurationError, match="must be one of"):
        load_settings(env)


@pytest.mark.parametrize(
    "name",
    [
        "DEVOPS_AGENT_AUTO_CREATE_SPACE",
        "ACP_BRIDGE_AWS_REQUIRE_SPACE_ID",
        "ACP_BRIDGE_AWS_ALLOW_SPACE_CREATE",
    ],
)
def test_invalid_security_boolean_fails_closed(name):
    env = _base_env() | {name: "maybe"}
    with pytest.raises(ConfigurationError, match=f"{name} must be a boolean"):
        load_settings(env)


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("root cause of outage", False),
        ("/investigate", False),
        ("/investigate   ", False),
        ("/investigation outage", False),
        (" /INVESTIGATE root cause of outage", True),
    ],
)
def test_explicit_investigation_command(prompt, expected):
    assert is_explicit_investigation(prompt) is expected


def test_server_subclass_preserves_all_three_modes(monkeypatch):
    package = types.ModuleType("aws_devops_agent")
    package.__path__ = []
    module = types.ModuleType("aws_devops_agent.acp_server")

    class FakeACPServer:
        @classmethod
        def _looks_like_investigation(cls, text):
            return text == "upstream-auto"

        def run(self):
            raise AssertionError("unit test must not start the server")

    module.ACPServer = FakeACPServer
    monkeypatch.setitem(sys.modules, "aws_devops_agent", package)
    monkeypatch.setitem(sys.modules, "aws_devops_agent.acp_server", module)

    explicit = build_server("explicit")
    assert explicit._looks_like_investigation("root cause of outage") is False
    assert explicit._looks_like_investigation("/investigate root cause") is True

    disabled = build_server("disabled")
    assert disabled._looks_like_investigation("/investigate root cause") is False

    automatic = build_server("auto")
    assert automatic._looks_like_investigation("upstream-auto") is True
