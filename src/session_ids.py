"""Default session id for callers that do not send one."""

import os
import uuid


def default_session_id(agent_name: str, cwd: str = "") -> str:
    """Session id used when the caller sends neither session_id nor channel_id.

    Without a cwd this is the historical one-session-per-agent id, so existing
    callers keep their multi-turn session. With a cwd the id is scoped to that
    directory: two callers working in different repos/worktrees on the same
    agent get separate sessions instead of fighting over one process and
    rebinding its cwd mid-run.
    """
    if not cwd:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, agent_name))
    norm = os.path.normpath(cwd)
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{agent_name}:cwd:{norm}"))
