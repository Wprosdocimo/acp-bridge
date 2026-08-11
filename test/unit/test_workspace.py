"""Unit tests for src/workspace.py — pipeline workspace TTL sweeper."""

import os
import time
from pathlib import Path

import pytest

from src import workspace

TTL = 72 * 3600
OLD = time.time() - 100 * 3600


def _mk(base: Path, rel: str, mtime: float | None = None) -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    if mtime:
        os.utime(p, (mtime, mtime))
        d = p.parent
        while d != base:
            os.utime(d, (mtime, mtime))
            d = d.parent
    return p


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(workspace, "_last_sweep", 0.0)


def test_removes_expired_pipeline_dirs(tmp_path):
    _mk(tmp_path, "sequence/pipeline-dead/out.md", OLD)
    _mk(tmp_path, "conversation/conv-dead/log.txt", OLD)
    removed = workspace.sweep(str(tmp_path), TTL, throttle=False)
    assert removed == 2
    assert not (tmp_path / "sequence/pipeline-dead").exists()
    assert not (tmp_path / "conversation/conv-dead").exists()


def test_keeps_fresh_dirs(tmp_path):
    _mk(tmp_path, "sequence/pipeline-fresh/out.md")
    assert workspace.sweep(str(tmp_path), TTL, throttle=False) == 0
    assert (tmp_path / "sequence/pipeline-fresh/out.md").exists()


def test_inner_fresh_file_protects_stale_dir(tmp_path):
    """Directory mtime is old but a nested file is fresh — must keep."""
    _mk(tmp_path, "sequence/pipeline-mixed/old.md", OLD)
    _mk(tmp_path, "sequence/pipeline-mixed/sub/new.md")
    os.utime(tmp_path / "sequence/pipeline-mixed", (OLD, OLD))
    assert workspace.sweep(str(tmp_path), TTL, throttle=False) == 0
    assert (tmp_path / "sequence/pipeline-mixed/sub/new.md").exists()


def test_skips_active_pipelines(tmp_path):
    d = _mk(tmp_path, "sequence/pipeline-act/out.md", OLD).parent
    removed = workspace.sweep(str(tmp_path), TTL, active={str(d)}, throttle=False)
    assert removed == 0
    assert d.exists()


def test_never_touches_foreign_paths(tmp_path):
    """Top-level user files, non-mode dirs, and wrong-prefix dirs survive."""
    _mk(tmp_path, "bonsai.txt", OLD)
    _mk(tmp_path, "build/artifact.bin", OLD)
    _mk(tmp_path, "sequence/user-stuff/keep.md", OLD)
    assert workspace.sweep(str(tmp_path), TTL, throttle=False) == 0
    assert (tmp_path / "bonsai.txt").exists()
    assert (tmp_path / "build/artifact.bin").exists()
    assert (tmp_path / "sequence/user-stuff/keep.md").exists()


def test_ttl_zero_disables(tmp_path):
    _mk(tmp_path, "sequence/pipeline-dead/out.md", OLD)
    assert workspace.sweep(str(tmp_path), 0, throttle=False) == 0
    assert (tmp_path / "sequence/pipeline-dead").exists()


def test_throttle_suppresses_repeat_sweep(tmp_path):
    _mk(tmp_path, "sequence/pipeline-dead1/out.md", OLD)
    assert workspace.sweep(str(tmp_path), TTL) == 1
    _mk(tmp_path, "sequence/pipeline-dead2/out.md", OLD)
    # second call within 30 min window is a no-op
    assert workspace.sweep(str(tmp_path), TTL) == 0
    assert (tmp_path / "sequence/pipeline-dead2").exists()


def test_missing_base_is_noop():
    assert workspace.sweep("/nonexistent/base", TTL, throttle=False) == 0
