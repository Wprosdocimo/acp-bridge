# Chat Mode

## Activate

`/chat ko` or `/chat cc [--cwd <path>]`

1. Generate a new random session_id (UUID v4) for this chat
2. Write `chat-state.json`:
   ```json
   {
     "active_agent": "kiro",
     "session_id": "00000000-0000-0000-0000-000000000001",
     "cwd": "/home/ec2-user/projects/acp-bridge",
     "started_at": "2025-01-15T10:30:00Z"
   }
   ```
3. Reply: `🟢 Entered kiro chat mode (session: xxx)`

## Forward

Subsequent messages auto-forwarded:

```bash
$ACP_CLIENT -a <active_agent> -s <session_id> [--cwd <cwd>] "<user message>"
```

## Exit

`/chat end` → Delete `chat-state.json`, reply: `🔴 Exited chat mode`

## Status

`/chat status` → Read `chat-state.json`, output agent, session, cwd, uptime.

## Switch

Run `/chat cc` directly to replace state; old session preserved on server.

## Session ID

- Each chat gets its own random UUID, kept in `chat-state.json`, so two chats
  on the same agent never share a process
- Without `-s`, the bridge derives the session from (agent, cwd): calls from the
  same directory reuse one session, calls from different directories do not
- Manual override: `$ACP_CLIENT -s <uuid> "<prompt>"`
