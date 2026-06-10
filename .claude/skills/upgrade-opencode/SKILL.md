---
name: upgrade-opencode
description: >-
  Check whether the OpenCode server that powers Balam is on the latest release,
  and upgrade it safely: compare the installed binary against the newest GitHub
  release, scan the changelog for HTTP/SSE API changes that could break the
  Balam backend, run `opencode upgrade`, then restart the systemd services.
  Use this whenever the user asks to "upgrade opencode", "update opencode",
  "bump opencode", asks "is opencode up to date / on the latest version", or
  reports agent breakage that smells like an OpenCode version mismatch. For
  plain start/stop/restart with no version change, use `run-balam` instead.
---

# Upgrade OpenCode (the agent behind Balam)

OpenCode is a separate process from this repo — `balam-opencode.service` runs
`/home/ubuntu/.opencode/bin/opencode serve` on `127.0.0.1:4096`, and the Balam
backend talks to it over its **raw HTTP/SSE API** (hand-written client in
`apps/backend/src/balam/opencode.py`, ADR-0002/0011). That raw-API coupling is
why this skill exists: an upgrade can silently change endpoints Balam depends
on, so check before and verify after.

Day-to-day service operation (start/stop/status/log semantics) is the
**`run-balam`** skill's domain — read `.claude/skills/run-balam/SKILL.md`
before the restart step. This skill only adds the version-management layer.

## 1. Check versions

```sh
/home/ubuntu/.opencode/bin/opencode --version          # what the service runs
curl -sL https://api.github.com/repos/anomalyco/opencode/releases/latest \
  | grep -m1 '"tag_name"'                              # latest release
```

Gotchas, learned the hard way:

- Always check the **service's** binary by full path. A bare `opencode` on
  `$PATH` can resolve to a different install; the path in
  `deploy/balam-opencode.service`'s `ExecStart` is the one that matters.
- The project moved from the `sst` GitHub org to **`anomalyco`**. The old
  `sst/opencode` API URL returns a "Moved Permanently" JSON stub instead of a
  release — query `anomalyco/opencode` (and keep `-L` for any future move).

If installed == latest, report that and stop — no restart needed.

## 2. Scan the changelog before touching anything

Record the current version (it is the rollback target), then read the release
notes for every version between installed and latest:
<https://github.com/anomalyco/opencode/releases>.

You are looking for changes to the **server/HTTP API** surface Balam consumes:
session endpoints, SSE event shapes, permission/approval flow, MCP server
registration, auth. Renamed tools or TUI changes don't matter; a renamed
endpoint does. If anything looks breaking, tell the user what you found and let
them decide before upgrading — this is their bot's brainstem.

## 3. Upgrade

```sh
/home/ubuntu/.opencode/bin/opencode upgrade            # latest
/home/ubuntu/.opencode/bin/opencode upgrade v1.16.4    # or pin a specific version
```

The binary is replaced in place, so the running service keeps the old code
until restarted. Confirm the new bits landed:

```sh
/home/ubuntu/.opencode/bin/opencode --version
```

## 4. Restart the services

A restart kills any in-flight agent session, so if the bot might be mid-task,
warn the user first. Then (per `run-balam` — `balam.service` *Requires* the
opencode unit, so restart both explicitly rather than relying on propagation):

```sh
sudo systemctl restart --no-block balam-opencode balam
```

Never run this *from inside the bot* (i.e., as the OpenCode agent over
Telegram) — it kills the process executing the command. From a normal shell or
Claude Code session it's fine.

## 5. Verify

```sh
systemctl --no-pager status balam-opencode balam   # both: active (running)
journalctl -u balam -n 50 --no-pager               # "Application started", no httpx errors
```

The real proof is a round-trip through Telegram — that's the **`browser-use`**
skill's job if the user wants it exercised.

## If the new version breaks Balam

Roll back to the version recorded in step 2 and restart again:

```sh
/home/ubuntu/.opencode/bin/opencode upgrade v<previous-version>
sudo systemctl restart --no-block balam-opencode balam
```

Then capture what broke (journal errors, the failing endpoint) in an issue or
ADR note so the next upgrade attempt starts from knowledge, not surprise.
