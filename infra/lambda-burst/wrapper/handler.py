"""Lambda wrapper for harness-factory ACP agent.

Deployed as Lambda function code. The harness-factory binary lives in a Lambda Layer
at /opt/bin/harness-factory.

Security:
  - API keys are stored in Secrets Manager, read at runtime (cached per warm start)
  - No secrets in environment variables or CloudFormation templates
  - Lambda runs in VPC with restricted egress (LiteLLM port + HTTPS only)

Protocol:
  - Lambda receives event with {profile, prompt, model, timeout, session_id}
  - Wrapper reads LiteLLM API key from Secrets Manager (cached)
  - Spawns harness-factory via stdio, speaks ACP JSON-RPC
  - Collects agent output, returns as Lambda response
"""

import json
import os
import subprocess
import time
import uuid

import boto3

# harness-factory binary path (from Lambda Layer)
HARNESS_BIN = os.environ.get("HARNESS_BIN", "/opt/bin/harness-factory")
DEFAULT_TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "300"))
LITELLM_URL = os.environ.get("LITELLM_URL", "")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "bedrock/anthropic.claude-sonnet-4-6")
SECRET_ARN = os.environ.get("LITELLM_SECRET_ARN", "")

# Cache secret across warm starts (Lambda container reuse)
_cached_api_key: str | None = None


def _get_api_key() -> str:
    """Read LiteLLM API key from Secrets Manager (cached per container)."""
    global _cached_api_key
    if _cached_api_key is not None:
        return _cached_api_key

    if not SECRET_ARN:
        raise ValueError("LITELLM_SECRET_ARN not set — cannot retrieve API key")

    client = boto3.client("secretsmanager")
    resp = client.get_secret_value(SecretId=SECRET_ARN)
    raw = resp["SecretString"]

    # Support both plain string and JSON {"apiKey": "..."} formats
    try:
        parsed = json.loads(raw)
        _cached_api_key = parsed.get("apiKey", parsed.get("api_key", raw))
    except (json.JSONDecodeError, TypeError):
        _cached_api_key = raw

    return _cached_api_key


def handler(event, context):
    """Lambda entry point.

    Event schema:
    {
      "prompt": "string - the task prompt",
      "profile": { ... },    // harness-factory profile JSON (optional)
      "model": "bedrock/...",  // optional, overrides DEFAULT_MODEL
      "timeout": 300,          // optional
      "session_id": "..."      // optional, for tracking
    }

    Response schema:
    {
      "status": "completed" | "error",
      "output": "string - agent output text",
      "duration": 12.3,
      "session_id": "...",
    }
    """
    t0 = time.time()
    prompt = event.get("prompt", "")
    profile = event.get("profile") or {}
    model = event.get("model", DEFAULT_MODEL)
    timeout = event.get("timeout", DEFAULT_TIMEOUT)
    session_id = event.get("session_id", str(uuid.uuid4()))

    # Warmup ping — return immediately without spawning agent
    if prompt == "__warmup__":
        return {
            "status": "completed",
            "output": "warm",
            "duration": round(time.time() - t0, 2),
            "session_id": session_id,
        }

    if not prompt:
        return {
            "status": "error",
            "error": "prompt is required",
            "output": "",
            "duration": 0,
            "session_id": session_id,
        }

    # Read API key from Secrets Manager
    try:
        api_key = _get_api_key()
    except Exception as e:
        return {
            "status": "error",
            "error": f"failed to read secret: {e}",
            "output": "",
            "duration": round(time.time() - t0, 2),
            "session_id": session_id,
        }

    # Inject LiteLLM config into profile
    profile.setdefault("agent", {})
    profile["agent"]["model"] = model
    profile["agent"]["litellm_url"] = LITELLM_URL
    profile["agent"]["litellm_api_key"] = api_key

    # Working directory (Lambda /tmp is writable, up to ephemeral storage size)
    cwd = f"/tmp/work/{session_id}"
    os.makedirs(cwd, exist_ok=True)

    try:
        output = _run_acp_session(prompt, profile, cwd, timeout)
        return {
            "status": "completed",
            "output": output,
            "duration": round(time.time() - t0, 2),
            "session_id": session_id,
        }
    except TimeoutError as e:
        return {
            "status": "error",
            "error": f"timeout after {timeout}s",
            "output": str(e),
            "duration": round(time.time() - t0, 2),
            "session_id": session_id,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "output": "",
            "duration": round(time.time() - t0, 2),
            "session_id": session_id,
        }
    finally:
        # Clean up workspace to avoid /tmp bloat across warm invocations
        import shutil
        shutil.rmtree(cwd, ignore_errors=True)


def _run_acp_session(prompt: str, profile: dict, cwd: str, timeout: int) -> str:
    """Spawn harness-factory, run ACP JSON-RPC session, collect output."""
    proc = subprocess.Popen(
        [HARNESS_BIN],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env={**os.environ, "HOME": "/tmp"},
    )

    try:
        # 1. initialize
        _send(proc, _rpc(1, "initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
            "clientInfo": {"name": "acp-bridge-lambda", "version": "1.0.0"},
        }))
        _recv_response(proc, msg_id=1, timeout=30)

        # 2. session/new with profile
        _send(proc, _rpc(2, "session/new", {
            "cwd": cwd,
            "mcpServers": [],
            "profile": profile,
        }))
        session_resp = _recv_response(proc, msg_id=2, timeout=30)
        acp_session_id = session_resp.get("result", {}).get("sessionId", "")

        # 3. session/prompt
        _send(proc, _rpc(3, "session/prompt", {
            "sessionId": acp_session_id,
            "prompt": [{"type": "text", "text": prompt}],
        }))

        # 4. Collect output
        output_parts = []
        deadline = time.time() + timeout

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"agent did not complete within {timeout}s")

            line = _readline(proc, timeout=min(5.0, remaining))
            if line is None:
                # Check if process died
                if proc.poll() is not None:
                    break
                continue

            msg = json.loads(line)

            # Handle agent requests
            msg_id = msg.get("id")
            method = msg.get("method", "")

            if method == "fs/read_text_file" and msg_id is not None:
                _reply_fs_read(proc, msg)
                continue
            elif method == "fs/write_text_file" and msg_id is not None:
                _reply_fs_write(proc, msg)
                continue
            elif method == "session/request_permission" and msg_id is not None:
                _reply_allow(proc, msg)
                continue

            # Notification: collect text output
            if "method" in msg and not msg.get("id"):
                params = msg.get("params", {})
                update = params.get("update", {})
                if update.get("sessionUpdate") == "agent_text_chunk":
                    output_parts.append(update.get("textChunk", ""))

            # Final response to prompt request (id=3)
            if msg.get("id") == 3:
                if "error" in msg:
                    err = msg["error"]
                    raise RuntimeError(f"agent error: {err.get('message', err)}")
                break

        return "".join(output_parts)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ──── JSON-RPC helpers ────


def _rpc(req_id: int, method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}


def _send(proc, msg: dict):
    data = json.dumps(msg) + "\n"
    proc.stdin.write(data.encode())
    proc.stdin.flush()


def _readline(proc, timeout: float = 5.0) -> str | None:
    """Read one line with timeout via select()."""
    import select
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if ready:
        line = proc.stdout.readline()
        if line:
            return line.decode().strip()
    return None


def _recv_response(proc, msg_id: int, timeout: float = 30) -> dict:
    """Read until we get a JSON-RPC response matching msg_id."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = _readline(proc, timeout=min(2.0, deadline - time.time()))
        if line is None:
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode()[:500]
                raise RuntimeError(f"harness-factory exited unexpectedly: {stderr}")
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == msg_id:
            if "error" in msg:
                raise RuntimeError(f"ACP error on id={msg_id}: {msg['error']}")
            return msg
    raise TimeoutError(f"No response for id={msg_id} within {timeout}s")


def _reply_fs_read(proc, msg: dict):
    path = msg.get("params", {}).get("path", "")
    try:
        with open(path) as f:
            content = f.read()
        _send(proc, {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": content}})
    except Exception:
        _send(proc, {"jsonrpc": "2.0", "id": msg["id"],
                     "result": {"content": f"ERROR: ENOENT: {path}"}})


def _reply_fs_write(proc, msg: dict):
    params = msg.get("params", {})
    fpath = params.get("path", "")
    content = params.get("content", "")
    try:
        dirn = os.path.dirname(fpath)
        if dirn:
            os.makedirs(dirn, exist_ok=True)
        with open(fpath, "w") as f:
            f.write(content)
        _send(proc, {"jsonrpc": "2.0", "id": msg["id"], "result": {}})
    except Exception as e:
        _send(proc, {"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": -1, "message": str(e)}})


def _reply_allow(proc, msg: dict):
    _send(proc, {"jsonrpc": "2.0", "id": msg["id"],
                 "result": {"outcome": {"outcome": "selected", "optionId": "proceed_always"}}})
