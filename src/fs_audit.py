"""fs_audit — SQLite audit trail for agent filesystem access.

Every fs/read_text_file and fs/write_text_file an agent issues is recorded
with its trust level and the allow/deny outcome, so operators can answer
"which agent, at what trust level, touched which file, when, and was it
allowed?". Denials (level-0 probing, level-1 blacklist hits) are recorded too.

Mirrors PromptStore's design: best-effort writes that never raise into the
agent call path; co-located in data/jobs.db; CREATE TABLE IF NOT EXISTS.
"""

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("acp-bridge.fs_audit")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fs_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    agent        TEXT NOT NULL,
    trust_level  INTEGER NOT NULL DEFAULT 0,
    session_id   TEXT DEFAULT '',
    operation    TEXT NOT NULL,
    path         TEXT NOT NULL,
    cwd          TEXT DEFAULT '',
    size         INTEGER NOT NULL DEFAULT 0,
    outcome      TEXT NOT NULL,
    deny_reason  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_fsaudit_ts ON fs_audit(ts);
CREATE INDEX IF NOT EXISTS idx_fsaudit_agent ON fs_audit(agent);
CREATE INDEX IF NOT EXISTS idx_fsaudit_outcome ON fs_audit(outcome);
CREATE INDEX IF NOT EXISTS idx_fsaudit_level ON fs_audit(trust_level);
"""


class FsAuditStore:
    def __init__(self, db_path: str = "data/jobs.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        log.info("fs_audit_init: db=%s", db_path)

    def record(
        self,
        *,
        agent: str,
        trust_level: int,
        operation: str,
        path: str,
        outcome: str,
        session_id: str = "",
        cwd: str = "",
        size: int = 0,
        deny_reason: str = "",
    ) -> None:
        """Persist one fs access record. Best-effort; never raises."""
        try:
            self._db.execute(
                """INSERT INTO fs_audit
                   (ts, agent, trust_level, session_id, operation, path, cwd,
                    size, outcome, deny_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(),
                    agent,
                    int(trust_level),
                    session_id,
                    operation,
                    path,
                    cwd,
                    int(size),
                    outcome,
                    deny_reason,
                ),
            )
            self._db.commit()
        except Exception as e:
            log.warning("fs_audit_insert_failed: agent=%s op=%s err=%s", agent, operation, e)

    def search(
        self,
        *,
        agent: str | None = None,
        trust_level: int | None = None,
        outcome: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[dict]:
        clauses, params = [], []
        if agent:
            clauses.append("agent=?")
            params.append(agent)
        if trust_level is not None:
            clauses.append("trust_level=?")
            params.append(int(trust_level))
        if outcome:
            clauses.append("outcome=?")
            params.append(outcome)
        if since is not None:
            clauses.append("ts>=?")
            params.append(float(since))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        try:
            rows = self._db.execute(
                f"SELECT * FROM fs_audit {where} ORDER BY ts DESC LIMIT ?", params
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            log.warning("fs_audit_search_failed: err=%s", e)
            return []

    def cleanup_older_than(self, retention_seconds: float) -> int:
        if retention_seconds <= 0:
            return 0
        try:
            cutoff = time.time() - retention_seconds
            cur = self._db.execute("DELETE FROM fs_audit WHERE ts < ?", (cutoff,))
            self._db.commit()
            n = cur.rowcount
            if n:
                log.info("fs_audit_cleanup: deleted=%d", n)
            return n
        except Exception as e:
            log.warning("fs_audit_cleanup_failed: err=%s", e)
            return 0
