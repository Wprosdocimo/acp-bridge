"""Unit tests for AcpConnection._pick_always_allow_option — the "always
allow" optionId picker used to auto-reply to session/request_permission.
"""

import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.acp_client import AcpConnection


def test_picks_allow_always_kind():
    options = [
        {"kind": "allow_always", "name": "Yes, always", "optionId": "allow_always"},
        {"kind": "allow_once", "name": "Allow", "optionId": "allow"},
        {"kind": "reject_once", "name": "Reject", "optionId": "reject"},
    ]
    assert AcpConnection._pick_always_allow_option(options) == "allow_always"


def test_falls_back_to_allow_once_kind_when_no_always_option():
    options = [
        {"kind": "allow_once", "name": "Allow", "optionId": "allow"},
        {"kind": "reject_once", "name": "Reject", "optionId": "reject"},
    ]
    assert AcpConnection._pick_always_allow_option(options) == "allow"


def test_falls_back_to_legacy_literal_when_options_empty():
    assert AcpConnection._pick_always_allow_option([]) == "proceed_always"


def test_ignores_malformed_entries():
    options = [
        "not-a-dict",
        {"kind": "allow_always", "optionId": "allow_always"},
    ]
    assert AcpConnection._pick_always_allow_option(options) == "allow_always"
