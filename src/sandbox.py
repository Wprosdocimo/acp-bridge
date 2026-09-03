"""Filesystem sandbox — three-tier trust model for agent fs access.

Agents talk to the Bridge over ACP stdio JSON-RPC and can issue
`fs/read_text_file` / `fs/write_text_file` requests with an arbitrary path.
The client-supplied `cwd` (via /runs metadata, pipeline shared_cwd) is also
attacker-controllable. Without bounds, any agent can read the host's secrets
(.env, ~/.aws/credentials) or write anywhere.

This module maps each agent to a trust tier and decides whether a given path
(or cwd) is allowed:

    level 0  sandboxed    — path must be within an allowed root
                            (agent working_dir + public_workdir + pipeline
                            base + upload_dir). Default when `trust` unset.
    level 1  workspace    — anything EXCEPT security-sensitive paths
                            (blacklist: ~/.aws, ~/.ssh, /etc, *.pem, .env, ...).
    level 2  unrestricted — no restriction. Emits a startup warning; every
                            fs op is audited. Must be set explicitly.

Path checks resolve realpath (defeating ../ traversal and symlink escape) and
match on path components (so /tmp/acp does not falsely contain /tmp/acp-public).
"""

import logging
import os
from fnmatch import fnmatch

log = logging.getLogger("acp-bridge.sandbox")

LEVEL_NAMES = {0: "sandboxed", 1: "workspace", 2: "unrestricted"}
NAME_LEVELS = {v: k for k, v in LEVEL_NAMES.items()}

# Default security-sensitive blacklist for level 1 (workspace) agents.
# Directory prefixes (no glob chars) match the dir and everything under it;
# glob patterns match the full realpath and the basename.
DEFAULT_BLACKLIST = [
    "~/.aws",
    "~/.ssh",
    "~/.gnupg",
    "~/.config/gcloud",
    "~/.kube",
    "~/.docker",
    "/etc",
    "/root",
    "/boot",
    "/sys",
    "/proc",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/credentials",
    "**/.netrc",
    "**/.git-credentials",
]


def _norm(p: str) -> str:
    """Absolute realpath with ~ expansion. Resolves symlinks and ../."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(p or "")))


def normalize_level(trust) -> int:
    """Coerce a config `trust` value (int or name) to a level int. Default 0."""
    if trust is None or trust == "":
        return 0
    if isinstance(trust, int):
        return trust if trust in LEVEL_NAMES else 0
    return NAME_LEVELS.get(str(trust).strip().lower(), 0)


class Sandbox:
    """Central policy object. One instance shared by the pool and connections."""

    def __init__(
        self,
        allowed_roots: list[str] | None = None,
        blacklist: list[str] | None = None,
        enabled: bool = True,
    ):
        self.enabled = enabled
        # Store normalized roots once; recomputed if roots are added.
        self._roots: list[str] = []
        for r in allowed_roots or []:
            self.add_root(r)
        self._blacklist = list(blacklist if blacklist is not None else DEFAULT_BLACKLIST)

    def add_root(self, root: str) -> None:
        if not root:
            return
        nr = _norm(root)
        if nr not in self._roots:
            self._roots.append(nr)

    @property
    def roots(self) -> list[str]:
        return list(self._roots)

    def in_roots(self, path: str, extra_roots: list[str] | None = None) -> bool:
        """True if realpath(path) is within any allowed root (component-wise)."""
        rp = _norm(path)
        # self._roots are already normalized (add_root); only extras need it.
        roots = self._roots if not extra_roots else self._roots + [_norm(r) for r in extra_roots]
        return any(rp == rr or rp.startswith(rr + os.sep) for rr in roots)

    def hits_blacklist(self, path: str) -> bool:
        """True if realpath(path) matches any blacklist entry."""
        rp = _norm(path)
        base = os.path.basename(rp)
        for pat in self._blacklist:
            if any(c in pat for c in "*?["):
                if fnmatch(rp, os.path.expanduser(pat)) or fnmatch(base, os.path.basename(pat)):
                    return True
            else:
                ep = _norm(pat)
                if rp == ep or rp.startswith(ep + os.sep):
                    return True
        return False

    def check_path(
        self, level: int, path: str, extra_roots: list[str] | None = None
    ) -> tuple[bool, str]:
        """Decide whether an fs op on `path` is allowed for a given trust level.

        Returns (allowed, deny_reason). deny_reason is "" when allowed.
        """
        if not self.enabled:
            return True, ""
        if level >= 2:
            return True, ""
        if level == 1:
            if self.hits_blacklist(path):
                return False, "blacklist"
            return True, ""
        # level 0
        if self.in_roots(path, extra_roots):
            return True, ""
        return False, "outside_roots"

    def check_cwd(self, level: int, cwd: str) -> tuple[bool, str]:
        """Validate a client-supplied cwd override before spawning.

        Same policy as check_path — the cwd itself must be a legal location,
        otherwise a caller could set cwd=/ and read anything under level 0.
        """
        if not cwd:
            return True, ""  # empty → falls back to config working_dir
        return self.check_path(level, cwd)
