---
name: balam-debug-session
description: >-
  Investigate buggy Balam bot sessions by correlating OpenCode session ids,
  conversation history, Balam backend logs, OpenCode server logs, Telegram
  topic/session mappings, and systemd journal output. Use when asked to debug a
  Balam bot issue, retrieve a ses_... OpenCode session, inspect conversation
  history, correlate error logs, investigate a live or historical Telegram bot
  session, or produce a local forensic report for a buggy Balam session.
---

# Debug Balam Sessions

Balam is a Telegram bot backed by an OpenCode server. Debugging a bot failure
usually means correlating three timelines:

- Telegram/Balam routing: chat, topic/thread, context, and stored session map.
- OpenCode persistence: `ses_...` session metadata, messages, parts, tool calls,
  and session diffs.
- Runtime logs: `balam.service`, `balam-opencode.service`, and sometimes
  `cloudflared-balam.service` journal output.

This skill is for agent-only local forensics. It may inspect full prompts, tool
output, logs, and local databases. Do not expose session ids in Telegram unless
the user explicitly asks.

## Inputs You May Receive

The user may provide any of these. Use all provided signals; do not require a
perfect `ses_...` id before starting.

- An OpenCode session id, for example `ses_15663b3e8ffeZzvw3hR6liWbRJ`.
- A Telegram chat id, topic/thread id, or message id.
- A rough timestamp or time range.
- An error message, traceback, or symptom.
- A request like "debug the latest Balam bot issue".
- A request from a new OpenCode conversation to inspect an older session.

If the input is ambiguous, first gather candidate sessions/log windows and report
confidence instead of asking immediately. Ask only when multiple plausible
sessions remain after local correlation.

## Important Local Paths

- Repo: `/home/ubuntu/projects/balam`
- OpenCode DB: `/home/ubuntu/.local/share/opencode/balam.db` (dedicated via
  `OPENCODE_DB` in `balam-opencode.service` since 2026-06-13; seeded from the
  previously shared `opencode.db`, so pre-split rows may belong to other
  servers — ivy's `:4097`, interactive runs)
- OpenCode session diffs:
  `/home/ubuntu/.local/share/opencode/storage/session_diff/*.json`
- OpenCode file logs: `/home/ubuntu/.local/share/opencode/log/*.log`
- OpenCode tool output: `/home/ubuntu/.local/share/opencode/tool-output/*`
- Balam backend package: `/home/ubuntu/projects/balam/apps/backend`

Systemd units used by the deployed stack:

- `balam.service`: Balam backend, Telegram bot, and Mini App server.
- `balam-opencode.service`: OpenCode server on `127.0.0.1:4096`.
- `cloudflared-balam.service`: public tunnel for the Mini App.

## Safety Rules

- Prefer read-only commands: `sqlite3`, `journalctl`, `systemctl status`, file
  reads, and searches.
- Do not restart services unless the user explicitly asks.
- Do not modify databases or logs.
- Full local output is allowed for this skill, but keep final responses focused
  on relevant excerpts and conclusions.
- Avoid dumping secrets from `.env`, auth files, or unrelated sessions.
- If a query may return full conversation text, scope it to the target session or
  time window first.

## Standard Workflow

### 1. Identify Candidate Sessions

If the user gives a `ses_...` id, verify it exists:

```sh
sqlite3 -header -column "/home/ubuntu/.local/share/opencode/balam.db" \
  "SELECT id, directory, title,
          datetime(time_created/1000, 'unixepoch', 'localtime') AS created,
          datetime(time_updated/1000, 'unixepoch', 'localtime') AS updated,
          agent, model
     FROM session
    WHERE id = '<SESSION_ID>';"
```

If the user gives no id, list recent Balam workspace sessions:

```sh
sqlite3 -header -column "/home/ubuntu/.local/share/opencode/balam.db" \
  "SELECT id, directory, title,
          datetime(time_created/1000, 'unixepoch', 'localtime') AS created,
          datetime(time_updated/1000, 'unixepoch', 'localtime') AS updated,
          agent, model
     FROM session
    WHERE directory = '/home/ubuntu/projects/balam'
    ORDER BY time_updated DESC
    LIMIT 20;"
```

If the user gives text from the conversation or an error string, search message
and part JSON for it. Escape single quotes in the search string.

```sh
sqlite3 -header -column "/home/ubuntu/.local/share/opencode/balam.db" \
  "SELECT DISTINCT session_id,
          datetime(time_updated/1000, 'unixepoch', 'localtime') AS updated
     FROM message
    WHERE data LIKE '%<TEXT_SNIPPET>%'
   UNION
   SELECT DISTINCT session_id,
          datetime(time_updated/1000, 'unixepoch', 'localtime') AS updated
     FROM part
    WHERE data LIKE '%<TEXT_SNIPPET>%'
    ORDER BY updated DESC;"
```

### 2. Inspect Conversation History

First list user and assistant message boundaries:

```sh
sqlite3 -header -column "/home/ubuntu/.local/share/opencode/balam.db" \
  "SELECT id, json_extract(data, '$.role') AS role,
          datetime(time_created/1000, 'unixepoch', 'localtime') AS created,
          datetime(time_updated/1000, 'unixepoch', 'localtime') AS updated
     FROM message
    WHERE session_id = '<SESSION_ID>'
    ORDER BY time_created;"
```

Preview text parts without dumping everything:

```sh
sqlite3 -header -column "/home/ubuntu/.local/share/opencode/balam.db" \
  "SELECT message_id,
          json_extract(data, '$.type') AS type,
          substr(json_extract(data, '$.text'), 1, 240) AS text_preview,
          datetime(time_created/1000, 'unixepoch', 'localtime') AS created
     FROM part
    WHERE session_id = '<SESSION_ID>'
      AND json_extract(data, '$.type') = 'text'
    ORDER BY time_created;"
```

When full conversation content is needed, query the specific session only:

```sh
sqlite3 -json "/home/ubuntu/.local/share/opencode/balam.db" \
  "SELECT message_id, time_created, time_updated, data
     FROM part
    WHERE session_id = '<SESSION_ID>'
    ORDER BY time_created;"
```

### 3. Inspect Session Diffs And Tool Output

Check whether a session diff exists:

```sh
ls "/home/ubuntu/.local/share/opencode/storage/session_diff/<SESSION_ID>.json"
```

Read it only if relevant to code changes or file diffs. Tool outputs may be
referenced from `part.data`; inspect only referenced files or files modified near
the incident time.

### 4. Correlate Balam Logs

Choose a time window around the session timestamps or user-provided incident
time. Start narrow, then expand.

```sh
journalctl -u balam --since "2026-06-09 03:20:00" --until "2026-06-09 03:40:00" --no-pager
```

Look for:

- `failed to handle message`
- tracebacks
- Telegram `409 Conflict`
- topic/context/session routing messages
- OpenCode request/stream errors
- Markdown/Telegram formatting errors
- timeout, connection refused, HTTP 401/403/5xx

If logs are large, filter after first reading the window:

```sh
journalctl -u balam --since "<START>" --until "<END>" --no-pager \
  | rg -i "error|exception|traceback|failed|timeout|409|opencode|session|thread|topic"
```

### 5. Correlate OpenCode Server Logs

Use the same time window:

```sh
journalctl -u balam-opencode --since "<START>" --until "<END>" --no-pager
```

Look for:

- session creation or message handling near the Balam log timestamp
- tool errors
- provider/model errors
- MCP registration failures
- auth failures from the backend
- SSE disconnects or stream aborts

Also check OpenCode's file logs if journald is insufficient:

```sh
rg -n "<SESSION_ID>|error|exception|traceback|failed|timeout" \
  "/home/ubuntu/.local/share/opencode/log"
```

### 6. Check Tunnel Logs Only When Relevant

The tunnel is relevant for Mini App, browser/noVNC, or public URL issues. It is
usually irrelevant for normal Telegram bot text round-trips.

```sh
journalctl -u cloudflared-balam --since "<START>" --until "<END>" --no-pager
```

### 7. Correlate Balam's Local Store

If the incident starts from a Telegram topic/thread, inspect the backend store
schema before querying. The database path is configured by Balam settings and may
be relative to the backend or deployment environment, so discover it from config,
logs, or code rather than guessing.

Useful code locations:

- `apps/backend/src/balam/store.py`
- `apps/backend/src/balam/router.py`
- `apps/backend/src/balam/bot.py`
- `apps/backend/src/balam/config.py`

Use `sqlite3 <store-db> .schema` first, then query topic/session mappings by
`chat_id`, `message_thread_id`, context name, or OpenCode session id.

## Matching Confidence

Report how the target session was identified:

- High confidence: exact user prompt appears in `part` text for the session, or
  Balam store maps the Telegram topic directly to the `ses_...` id.
- Medium confidence: session timestamp and Balam/OpenCode log activity align,
  but no direct text/topic mapping was found.
- Low confidence: only recency or broad time-window matching is available.

When multiple candidates remain, show a short table with ids, titles,
directories, update times, and why each matched.

## Final Report Format

Keep the final response concise but actionable:

- Target: session id, title, directory, created/updated time, model/agent if
  known.
- Matching confidence: exact match, topic mapping, time-window match, or
  heuristic.
- Conversation timeline: key user prompts and assistant/tool phases relevant to
  the bug.
- Error timeline: relevant Balam/OpenCode/systemd log excerpts with timestamps.
- Findings: likely root cause, contributing factors, and affected component.
- Next steps: concrete commands, code areas, or tests to run.

If no bug is found, say what was checked and what evidence is missing.

## Common Pitfalls

- `.git/opencode` is a project/snapshot id, not a `ses_...` session id.
- OpenCode plain log files may not contain prompt text; the SQLite `message` and
  `part` tables usually do.
- SQL searches over `part.data` can match tool-call payloads from the current
  investigation. Prefer message role/type boundaries and exact user text parts
  when proving a session match.
- The most recent Balam workspace session is not necessarily the current chat
  session. Prove it by matching conversation text or topic/session mappings.
- Telegram bot polling is singleton; `409 Conflict` means another poller is
  running.
- Tunnel logs usually do not explain normal Telegram text bot failures.
