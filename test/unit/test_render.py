"""Unit tests for src/render.py + POST /pipelines template rendering (v0.37.0).

Two layers:
1. Pure functions — recursive substitution, scope precedence, unresolved
   detection with JSON paths, artifact extraction/resolution.
2. Route wiring — POST /pipelines renders before submit, returns 400 on
   unresolved variables, and stays byte-identical to v0.36.2 when no
   input/vars are supplied.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import pytest
from fastapi import FastAPI

from src.render import (
    build_scope,
    extract_artifacts,
    format_missing,
    render_payload,
    resolve_artifacts,
)
from src.routes import pipelines as pipelines_routes


# ============================================================================
# Pure functions
# ============================================================================

def test_render_is_recursive_into_nested_context():
    steps = [{"agent": "harness", "prompt": "idea: {{input}} -> gdd-{{uid}}.md"}]
    context = {
        "shared_cwd": "/tmp/opengame",
        "next": {"mode": "sequence", "steps": [
            {"agent": "qwen", "prompt": "upload to s3://{{bucket}}/{{uid}}/index.html"},
        ]},
    }
    steps, context, uid, missing = render_payload(steps, context, "racing game",
                                                  {"bucket": "b1"})
    assert missing == []
    assert steps[0]["prompt"] == f"idea: racing game -> gdd-{uid}.md"
    # 4 levels deep: context.next.steps[0].prompt
    assert context["next"]["steps"][0]["prompt"] == f"upload to s3://b1/{uid}/index.html"


def test_uid_is_consistent_across_the_whole_tree():
    steps = [{"agent": "a", "prompt": "{{uid}}"}, {"agent": "b", "prompt": "{{uid}}"}]
    context = {"next": {"steps": [{"agent": "c", "prompt": "{{uid}}"}]}}
    steps, context, uid, _ = render_payload(steps, context, "x", {})
    assert steps[0]["prompt"] == steps[1]["prompt"] == uid
    assert context["next"]["steps"][0]["prompt"] == uid


def test_uid_is_full_entropy_and_differs_per_call():
    _, _, uid1, _ = render_payload([], {}, "x", {})
    _, _, uid2, _ = render_payload([], {}, "x", {})
    assert len(uid1) == 8 and int(uid1, 16) >= 0  # 4 bytes hex
    assert uid1 != uid2


def test_scope_precedence_vars_override_auto_and_input():
    scope = build_scope("from-input", {"uid": "FORCED", "date": "1999-01-01",
                                       "input": "from-vars"})
    assert scope["uid"] == "FORCED"
    assert scope["date"] == "1999-01-01"
    assert scope["input"] == "from-vars"


def test_explicit_uid_is_honoured():
    _, _, uid, _ = render_payload([{"agent": "a", "prompt": "{{uid}}"}], {},
                                  "x", {}, uid="REUSED")
    assert uid == "REUSED"


def test_unresolved_variables_are_reported_with_paths_and_left_verbatim():
    steps = [{"agent": "a", "prompt": "ok"},
             {"agent": "b", "prompt": "{{distribution_id}} and {{nope}}"}]
    context = {"next": {"steps": [{"agent": "c", "prompt": "{{alsomissing}}"}]}}
    steps, _context, _uid, missing = render_payload(steps, context, "x", {})
    names = {m[0] for m in missing}
    paths = [m[1] for m in missing]
    assert names == {"distribution_id", "nope", "alsomissing"}
    assert "steps[1].prompt" in paths
    assert any("next.steps[0].prompt" in p for p in paths)
    # left verbatim, never blanked — the 400 body is the only consumer
    assert "{{nope}}" in steps[1]["prompt"]


def test_format_missing_dedupes_and_names_paths():
    msg = format_missing([("cdn", "steps[0].prompt"), ("cdn", "steps[0].prompt"),
                          ("cdn", "steps[1].prompt")])
    assert msg.count("{{cdn}}") == 2
    assert "steps[0].prompt" in msg and "steps[1].prompt" in msg


def test_non_string_scalars_survive_rendering():
    steps, _c, _u, _m = render_payload(
        [{"agent": "a", "prompt": "{{input}}", "timeout": 300, "output_as": ""}],
        {}, "hi", {})
    assert steps[0]["timeout"] == 300 and isinstance(steps[0]["timeout"], int)
    assert steps[0]["prompt"] == "hi"


def test_extract_artifacts_aligns_with_steps_and_strips_field():
    steps = [
        {"agent": "harness", "prompt": "a",
         "artifact": {"type": "file", "label": "GDD", "pattern": "gdd-1.md"}},
        {"agent": "opengame", "prompt": "b"},
        {"agent": "kiro", "prompt": "c", "artifact": None},
    ]
    artifacts = extract_artifacts(steps)
    assert len(artifacts) == 3
    assert artifacts[0]["pattern"] == "gdd-1.md"
    assert artifacts[1] is None and artifacts[2] is None
    assert all("artifact" not in s for s in steps)


def test_resolve_artifacts_finds_files_in_shared_cwd():
    cwd = tempfile.mkdtemp()
    with open(os.path.join(cwd, "gdd-abcd.md"), "w") as f:
        f.write("x")
    artifacts = [{"type": "file", "label": "GDD", "pattern": "gdd-abcd.md"},
                 None,
                 {"type": "url", "label": "CDN", "pattern": "https://cdn.example/g/"}]
    steps = [{"agent": "harness"}, {"agent": "opengame"}, {"agent": "kiro"}]
    out = resolve_artifacts(artifacts, steps, cwd)
    assert [e["step"] for e in out] == [0, 2]
    assert out[0]["exists"] is True
    assert out[0]["path"] == os.path.join(cwd, "gdd-abcd.md")
    assert out[0]["agent"] == "harness"
    assert out[1]["url"] == "https://cdn.example/g/"


def test_resolve_artifacts_marks_missing_file():
    artifacts = [{"type": "file", "label": "GDD", "pattern": "nope-*.md"}]
    out = resolve_artifacts(artifacts, [{"agent": "harness"}], tempfile.mkdtemp())
    assert out[0]["exists"] is False and "path" not in out[0]


# ============================================================================
# Route wiring
# ============================================================================

class _FakePipeline:
    def __init__(self, mode, steps, context):
        self.pipeline_id = "pl-test"
        self.status = "pending"
        self.mode = mode
        self.steps = steps
        self.context = context

    def to_dict(self):
        return {"pipeline_id": self.pipeline_id, "mode": self.mode,
                "status": self.status,
                "steps": [{"agent": s["agent"], "status": "completed"}
                          for s in self.steps]}


class _FakePipelineManager:
    def __init__(self):
        self.submitted = None
        self.pipeline = None

    def submit(self, mode, steps, context=None, webhook_meta=None):
        self.submitted = {"mode": mode, "steps": steps, "context": context or {}}
        self.pipeline = _FakePipeline(mode, steps, context or {})
        return self.pipeline

    def get(self, pipeline_id):
        return self.pipeline if self.pipeline else None

    def get_transcript(self, pipeline_id):
        return []


def _app():
    mgr = _FakePipelineManager()
    app = FastAPI()
    pipelines_routes.register(app, mgr)
    return app, mgr


async def _post(app, payload):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/pipelines", json=payload)


@pytest.mark.asyncio
async def test_route_renders_payload_before_submit():
    app, mgr = _app()
    resp = await _post(app, {
        "mode": "sequence",
        "input": "racing game",
        "vars": {"bucket": "opengame-demo"},
        "context": {"shared_cwd": "/tmp/opengame"},
        "steps": [{"agent": "harness", "prompt": "idea: {{input}} -> gdd-{{uid}}.md"},
                  {"agent": "qwen", "prompt": "s3://{{bucket}}/{{uid}}/"}],
    })
    assert resp.status_code == 200
    uid = resp.json()["uid"]
    submitted = mgr.submitted["steps"]
    assert submitted[0]["prompt"] == f"idea: racing game -> gdd-{uid}.md"
    assert submitted[1]["prompt"] == f"s3://opengame-demo/{uid}/"
    assert mgr.submitted["context"]["_uid"] == uid


@pytest.mark.asyncio
async def test_route_rejects_unresolved_variables_and_submits_nothing():
    app, mgr = _app()
    resp = await _post(app, {
        "mode": "sequence",
        "input": "racing game",
        "steps": [{"agent": "harness", "prompt": "upload to s3://{{bucket}}/"}],
    })
    assert resp.status_code == 400
    assert "{{bucket}}" in resp.json()["error"]
    assert "steps[0].prompt" in resp.json()["error"]
    assert mgr.submitted is None  # nothing was queued


@pytest.mark.asyncio
async def test_route_is_backward_compatible_without_input_or_vars():
    """No input/vars -> no rendering at all; literal {{uid}} passes through."""
    app, mgr = _app()
    resp = await _post(app, {
        "mode": "sequence",
        "steps": [{"agent": "harness", "prompt": "literal {{uid}} stays"}],
    })
    assert resp.status_code == 200
    assert "uid" not in resp.json()
    assert mgr.submitted["steps"][0]["prompt"] == "literal {{uid}} stays"
    assert "_uid" not in mgr.submitted["context"]
    assert "_artifacts" not in mgr.submitted["context"]


@pytest.mark.asyncio
async def test_route_strips_artifact_from_steps_and_stashes_in_context():
    app, mgr = _app()
    resp = await _post(app, {
        "mode": "sequence",
        "input": "racing game",
        "steps": [
            {"agent": "harness", "prompt": "{{input}}",
             "artifact": {"type": "file", "label": "GDD", "pattern": "gdd-{{uid}}.md"}},
            {"agent": "opengame", "prompt": "build"},
        ],
    })
    assert resp.status_code == 200
    uid = resp.json()["uid"]
    # pipeline.submit() must not see the artifact key — src/pipeline.py is untouched
    assert all("artifact" not in s for s in mgr.submitted["steps"])
    artifacts = mgr.submitted["context"]["_artifacts"]
    assert artifacts[0]["pattern"] == f"gdd-{uid}.md" and artifacts[1] is None
    assert resp.json()["artifacts"][0]["label"] == "GDD"


@pytest.mark.asyncio
async def test_route_does_not_mutate_caller_context_dict():
    """context.copy() before injecting _artifacts/_uid."""
    app, mgr = _app()
    resp = await _post(app, {
        "mode": "sequence",
        "input": "x",
        "context": {"shared_cwd": "/tmp/opengame"},
        "steps": [{"agent": "a", "prompt": "{{input}}"}],
    })
    assert resp.status_code == 200
    assert mgr.submitted["context"]["shared_cwd"] == "/tmp/opengame"


@pytest.mark.asyncio
async def test_get_pipeline_resolves_artifacts_from_shared_cwd():
    app, mgr = _app()
    cwd = tempfile.mkdtemp()
    resp = await _post(app, {
        "mode": "sequence",
        "input": "racing",
        "context": {"shared_cwd": cwd},
        "steps": [{"agent": "harness", "prompt": "{{input}}",
                   "artifact": {"type": "file", "label": "GDD",
                                "pattern": "gdd-{{uid}}.md"}}],
    })
    uid = resp.json()["uid"]
    with open(os.path.join(cwd, f"gdd-{uid}.md"), "w") as f:
        f.write("done")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        got = await client.get("/pipelines/pl-test")
    body = got.json()
    assert body["uid"] == uid
    assert body["artifacts"][0]["exists"] is True
    assert body["artifacts"][0]["path"].endswith(f"gdd-{uid}.md")


@pytest.mark.asyncio
async def test_conversation_mode_renders_topic_and_initial_context():
    app, mgr = _app()
    resp = await _post(app, {
        "mode": "conversation",
        "input": "microservices vs monolith",
        "vars": {"style": "socratic"},
        "participants": ["kiro", "claude"],
        "topic": "debate: {{input}}",
        "initial_context": "style={{style}}, run={{uid}}",
    })
    assert resp.status_code == 200
    uid = resp.json()["uid"]
    ctx = mgr.submitted["context"]
    assert ctx["topic"] == "debate: microservices vs monolith"
    assert ctx["initial_context"] == f"style=socratic, run={uid}"
