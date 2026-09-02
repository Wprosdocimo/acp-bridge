"""Unit tests for the three-tier filesystem sandbox (v0.46.0).

Covers src/sandbox.py policy decisions and src/fs_audit.py persistence, plus
the AcpConnection fs enforcement path (read/write allow/deny + audit records).
"""

import os

import pytest

from src.fs_audit import FsAuditStore
from src.sandbox import DEFAULT_BLACKLIST, Sandbox, normalize_level


# ---------------------------------------------------------------------------
# normalize_level
# ---------------------------------------------------------------------------
class TestNormalizeLevel:
    def test_default_when_unset(self):
        assert normalize_level(None) == 0
        assert normalize_level("") == 0

    def test_names(self):
        assert normalize_level("sandboxed") == 0
        assert normalize_level("workspace") == 1
        assert normalize_level("unrestricted") == 2

    def test_case_insensitive(self):
        assert normalize_level("Workspace") == 1
        assert normalize_level("UNRESTRICTED") == 2

    def test_ints(self):
        assert normalize_level(0) == 0
        assert normalize_level(1) == 1
        assert normalize_level(2) == 2

    def test_unknown_falls_back_to_zero(self):
        assert normalize_level("bogus") == 0
        assert normalize_level(99) == 0


# ---------------------------------------------------------------------------
# Level 0 — sandboxed
# ---------------------------------------------------------------------------
class TestLevel0:
    @pytest.fixture
    def sb(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        return Sandbox(allowed_roots=[str(root)], enabled=True), str(root)

    def test_inside_root_allowed(self, sb):
        s, root = sb
        ok, reason = s.check_path(0, os.path.join(root, "file.txt"))
        assert ok and reason == ""

    def test_root_itself_allowed(self, sb):
        s, root = sb
        assert s.check_path(0, root)[0]

    def test_outside_denied(self, sb):
        s, root = sb
        ok, reason = s.check_path(0, "/etc/passwd")
        assert not ok and reason == "outside_roots"

    def test_traversal_escape_denied(self, sb):
        s, root = sb
        ok, _ = s.check_path(0, os.path.join(root, "../secret.txt"))
        assert not ok

    def test_symlink_escape_denied(self, sb, tmp_path):
        s, root = sb
        outside = tmp_path / "secret.txt"
        outside.write_text("x")
        link = os.path.join(root, "link")
        os.symlink(str(outside), link)
        assert not s.check_path(0, link)[0]

    def test_extra_root_allows_shared_cwd(self, sb, tmp_path):
        s, _ = sb
        pipeline_cwd = tmp_path / "pipeline-abc"
        pipeline_cwd.mkdir()
        target = os.path.join(str(pipeline_cwd), "out.txt")
        # not in global roots
        assert not s.check_path(0, target)[0]
        # but allowed when the connection's own cwd is passed as extra root
        assert s.check_path(0, target, extra_roots=[str(pipeline_cwd)])[0]

    def test_component_not_string_prefix(self):
        # /tmp/acp must not falsely contain /tmp/acp-public
        s = Sandbox(allowed_roots=["/tmp/acp"], enabled=True)
        assert not s.check_path(0, "/tmp/acp-public/x")[0]


# ---------------------------------------------------------------------------
# Level 1 — workspace (blacklist)
# ---------------------------------------------------------------------------
class TestLevel1:
    @pytest.fixture
    def sb(self):
        return Sandbox(allowed_roots=[], blacklist=DEFAULT_BLACKLIST, enabled=True)

    def test_normal_file_allowed(self, sb):
        assert sb.check_path(1, "/home/someone/project/main.py")[0]

    def test_env_denied(self, sb):
        ok, reason = sb.check_path(1, "/home/someone/project/.env")
        assert not ok and reason == "blacklist"

    def test_pem_denied(self, sb):
        assert not sb.check_path(1, "/home/someone/certs/server.pem")[0]

    def test_aws_dir_denied(self, sb):
        assert not sb.check_path(1, "~/.aws/credentials")[0]

    def test_ssh_key_denied(self, sb):
        assert not sb.check_path(1, "~/.ssh/id_rsa")[0]

    def test_etc_denied(self, sb):
        assert not sb.check_path(1, "/etc/shadow")[0]

    def test_credentials_file_denied(self, sb):
        assert not sb.check_path(1, "/home/x/app/credentials")[0]


# ---------------------------------------------------------------------------
# Level 2 — unrestricted
# ---------------------------------------------------------------------------
class TestLevel2:
    def test_anything_allowed(self):
        s = Sandbox(allowed_roots=[], enabled=True)
        assert s.check_path(2, "/etc/shadow")[0]
        assert s.check_path(2, "~/.aws/credentials")[0]


# ---------------------------------------------------------------------------
# Disabled sandbox
# ---------------------------------------------------------------------------
class TestDisabled:
    def test_disabled_allows_all(self):
        s = Sandbox(allowed_roots=["/tmp/x"], enabled=False)
        assert s.check_path(0, "/etc/passwd")[0]
        assert s.check_path(1, "/home/x/.env")[0]


# ---------------------------------------------------------------------------
# cwd admission
# ---------------------------------------------------------------------------
class TestCwdAdmission:
    def test_empty_cwd_allowed(self):
        s = Sandbox(allowed_roots=["/tmp/x"], enabled=True)
        assert s.check_cwd(0, "")[0]  # falls back to config working_dir

    def test_root_cwd_denied_level0(self):
        s = Sandbox(allowed_roots=["/tmp/x"], enabled=True)
        assert not s.check_cwd(0, "/")[0]

    def test_level1_cwd_not_blacklisted_allowed(self):
        s = Sandbox(allowed_roots=[], enabled=True)
        assert s.check_cwd(1, "/home/x/project")[0]

    def test_level1_cwd_blacklisted_denied(self):
        s = Sandbox(allowed_roots=[], enabled=True)
        assert not s.check_cwd(1, "/etc")[0]


# ---------------------------------------------------------------------------
# fs_audit store
# ---------------------------------------------------------------------------
class TestFsAudit:
    @pytest.fixture
    def store(self, tmp_path):
        return FsAuditStore(db_path=str(tmp_path / "audit.db"))

    def test_record_and_search(self, store):
        store.record(
            agent="kiro", trust_level=1, operation="read", path="/x/y.txt",
            outcome="allowed", cwd="/x", size=10,
        )
        rows = store.search()
        assert len(rows) == 1
        r = rows[0]
        assert r["agent"] == "kiro"
        assert r["trust_level"] == 1
        assert r["operation"] == "read"
        assert r["outcome"] == "allowed"

    def test_filter_by_outcome(self, store):
        store.record(agent="a", trust_level=0, operation="read", path="/ok", outcome="allowed")
        store.record(
            agent="a", trust_level=0, operation="read", path="/etc/passwd",
            outcome="denied", deny_reason="outside_roots",
        )
        denied = store.search(outcome="denied")
        assert len(denied) == 1
        assert denied[0]["deny_reason"] == "outside_roots"

    def test_filter_by_agent_and_level(self, store):
        store.record(agent="kiro", trust_level=1, operation="write", path="/p", outcome="allowed")
        store.record(agent="claude", trust_level=0, operation="write", path="/q", outcome="allowed")
        assert len(store.search(agent="kiro")) == 1
        assert len(store.search(trust_level=0)) == 1

    def test_cleanup(self, store):
        store.record(agent="a", trust_level=0, operation="read", path="/x", outcome="allowed")
        # retention 0 → no-op
        assert store.cleanup_older_than(0) == 0
        # everything older than -1s (i.e. future cutoff) gets deleted
        import time
        time.sleep(0.01)
        deleted = store.cleanup_older_than(0.001)
        assert deleted == 1


# ---------------------------------------------------------------------------
# AcpConnection fs enforcement (integration of sandbox + audit into the conn)
# ---------------------------------------------------------------------------
class TestConnectionFsEnforcement:
    def _make_conn(self, tmp_path, trust_level, audit):
        from unittest.mock import MagicMock

        from src.acp_client import AcpConnection

        root = tmp_path / "ws"
        root.mkdir()
        sb = Sandbox(allowed_roots=[str(root)], enabled=True)
        proc = MagicMock()
        sent = []
        proc.stdin.write = lambda d: sent.append(d)
        proc.stdin.drain = MagicMock()
        conn = AcpConnection(
            agent="kiro", session_id="s1", proc=proc,
            cwd=str(root), trust_level=trust_level, sandbox=sb, fs_audit=audit,
        )
        return conn, str(root), sent

    def test_level0_read_inside_allowed(self, tmp_path):
        audit = FsAuditStore(db_path=str(tmp_path / "a.db"))
        conn, root, _ = self._make_conn(tmp_path, 0, audit)
        allowed, reason = conn._fs_check("read", os.path.join(root, "f.txt"))
        assert allowed
        assert audit.search()[0]["outcome"] == "allowed"

    def test_level0_read_outside_denied_and_audited(self, tmp_path):
        audit = FsAuditStore(db_path=str(tmp_path / "a.db"))
        conn, _, _ = self._make_conn(tmp_path, 0, audit)
        allowed, reason = conn._fs_check("read", "/etc/passwd")
        assert not allowed and reason == "outside_roots"
        rec = audit.search()[0]
        assert rec["outcome"] == "denied"
        assert rec["deny_reason"] == "outside_roots"

    def test_level1_env_denied(self, tmp_path):
        audit = FsAuditStore(db_path=str(tmp_path / "a.db"))
        conn, _, _ = self._make_conn(tmp_path, 1, audit)
        allowed, reason = conn._fs_check("read", "/home/x/proj/.env")
        assert not allowed and reason == "blacklist"
