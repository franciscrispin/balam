# Deploy — Balam under systemd + Cloudflare tunnel (ADR-0013)

Runs the three Balam services under systemd and exposes **only** the Mini App to
the internet through a Cloudflare **named tunnel** (stable hostname), with Telegram
`initData` (ADR-0008) as the trust boundary. Read **ADR-0013** first — it states
the security conditions this setup enforces.

## What runs

| Unit                        | What it does                                              |
| --------------------------- | -------------------------------------------------------- |
| `balam-opencode.service`    | `opencode serve` on `127.0.0.1:4096` (the agent) — **only** when `AGENT_BACKEND=opencode` |
| `balam.service`             | `uv run balam` — the bot **and** the Mini App API/server on `127.0.0.1:$BALAM_PORT` |
| `cloudflared-balam.service` | named tunnel: `https://<your-host>` → that port only (`/etc/cloudflared/balam.yml`) |

The frontend is **not** a runtime service: `bun run build` produces `apps/frontend/dist`,
which FastAPI serves. OpenCode (`:4096`) and any VNC ports are **never** tunneled.

This is one bot on one Claude account. To run a second bot on a **second Claude
account** on the same VM, see [A second instance, on a second Claude
account](#a-second-instance-on-a-second-claude-account) below.

## In-Telegram Mini App: direct link

Telegram allows the in-app `web_app` button **only in private chats**. Balam is
scoped to a forum **supergroup**, so `/diff` instead sends a **direct Mini App link**
`t.me/<bot>/<shortname>?startapp=diff__<context>`, which opens the app inside
Telegram's webview (with signed `initData`) in any chat type. This needs a
BotFather-registered Mini App and a **stable** hostname (hence the named tunnel —
the BotFather Web App URL is fixed).

## Unit files are rendered, not copied

systemd units are static files — no variables, no "this user's home". So the
units here are **templates**: `balam.service.in`, `balam@.service.in`, and so on,
carrying `@USER@` / `@HOME@` / `@REPO@` / `@PATH@` placeholders. The install
scripts render them into `/etc/systemd/system` through `lib.sh`. Nothing in this
directory is tied to one host, and there is no per-machine fork of the units to
keep in sync.

`lib.sh` **derives** every value from the machine: the OS user running the
installer, that user's home, the checkout the script itself lives in, and
`command -v uv` / `bun` / `cloudflared`. The unit `PATH` is probed from the
toolchains actually installed under that home (bun, `.local/bin`, the highest
nvm node, cargo, go) — systemd sources no profile, so anything missing from that
line is missing for every shell the agent's Bash tool spawns.

A typical host therefore needs **no configuration at all**. To override
something — a different OS user, instances checked out elsewhere, a pinned
`PATH`, which Claude account the first bot runs as — copy
`deploy.env.example` to `deploy/deploy.env` (git-ignored) and set only what
differs. It holds paths, never secrets; those stay in the repo `.env`.

Rendered units carry a `# GENERATED …` header. Editing one in `/etc/` is
pointless — the next install overwrites it. Edit the template or `deploy.env`
and re-run the installer.

### Which Claude account the first bot runs as

`BALAM_PRIMARY_CLAUDE_CONFIG_DIR` is worth calling out. Unset — the default —
leaves `balam.service` on `~/.claude`, which is right when the machine runs one
bot and nobody uses `claude` interactively on it.

Set it when `~/.claude` is a **person's** login rather than the bot's. Otherwise
the bot's account is whatever that person last ran `claude auth login` as, and
the two share one rate limit. Extra instances are always pinned, by
`balam@.service`.

## One-time setup (creates public / account state — not in `install.sh`)

1. **Named tunnel + DNS** (stable hostname):
   ```sh
   cloudflared tunnel create balam
   cloudflared tunnel route dns balam <your-host>      # e.g. balam.example.com
   ```
   Copy `cloudflared-balam.example.yml` to `cloudflared-balam.yml` (git-ignored) and
   fill in the tunnel id + hostname (ingress → `127.0.0.1:3000`).
2. **BotFather Mini App** (`/newapp` on your bot): Web App URL = `https://<your-host>/`,
   pick a short name (e.g. `diff`).
3. **`deploy/balam.env`** (git-ignored — public-mode overlay):
   ```
   BALAM_PUBLIC_URL=https://<your-host>
   BALAM_MINIAPP_SHORTNAME=diff
   ```

The Mini App API always requires valid Telegram `initData` (ADR-0008/0013) — there
is no auth bypass. Both backend units also read the repo-root `.env` (bot token,
OpenCode password).

## Install / start

```sh
deploy/install.sh
```

Renders the units into `/etc/systemd/system`, installs
`/etc/cloudflared/balam.yml`, builds the Mini App, and starts the bot + tunnel —
plus `balam-opencode.service`, but **only** when the repo `.env` says
`AGENT_BACKEND=opencode`. In `claude_sdk` mode the agent runs in-process, so that
unit is neither installed nor ordered against; installing it anyway would leave a
service that fails on every boot.

## Operate

```sh
systemctl status balam balam-opencode cloudflared-balam
journalctl -u balam -f                 # bot + Mini App logs
journalctl -u cloudflared-balam -f     # tunnel logs
sudo systemctl restart balam           # after editing code / .env / balam.env
```

## A second instance, on a second Claude account

One VM can run several Balam bots, each signed in to a different Claude account.
The first instance stays exactly as described above. Each extra instance is a
**second checkout** with its own bot, its own port, and its own Claude login.

What makes the accounts separate is one environment variable. The `claude` CLI
reads its whole identity from `CLAUDE_CONFIG_DIR` (default `~/.claude`): the
saved login in `.credentials.json`, plus `.claude.json`, `projects/`,
`sessions/`, `settings.json`, `skills/` and `plugins/`. The Agent SDK starts the
CLI with the unit's environment, so setting the variable in the unit is enough.
Balam needs no code change, and this works only for `AGENT_BACKEND=claude_sdk`.

### Layout, by convention

For an instance named `<name>` (lowercase letters, digits and dashes):

| Thing        | Path or name                       | Notes                                          |
| ------------ | ---------------------------------- | ---------------------------------------------- |
| checkout     | `<next to the primary>-<name>`     | own `.env`, `config.yaml`, `balam.sqlite`      |
| Claude account | `~/.claude-<name>`               | own login, settings, skills, sessions          |
| bot service  | `balam@<name>.service`             | from the `balam@.service` template             |
| tunnel       | `cloudflared-balam@<name>.service` | optional; ingress `/etc/cloudflared/balam-<name>.yml` |

The instance name reaches the unit through systemd's `%i`, and the surrounding
paths are rendered from the machine — so the checkout lands beside the primary
(`/srv/balam` → `/srv/balam-work`), and `BALAM_INSTANCE_ROOT` /
`BALAM_INSTANCE_PREFIX` in `deploy.env` move it if you want it elsewhere.

### Install

```sh
deploy/install-instance.sh <name>
```

Run it once with the name you want. The first run creates the checkout (a local
clone of the primary checkout, with `origin` repointed at the real remote), copies
`.env.example` and `config.example.yaml` into place, and then stops and lists
what you must fill in. Fill those in and run it again.

Before it starts anything, the script refuses an instance that would break
quietly:

- **A reused bot token.** Two pollers on one token make Telegram answer `409
  Conflict`, and both bots stop receiving messages. Each instance needs its own
  BotFather bot.
- **A shared `BALAM_PORT`.** The Mini App server binds `127.0.0.1:BALAM_PORT`,
  and the port has one owner.
- **`CLAUDE_CONFIG_DIR` set in the instance `.env`.** systemd lets an
  `EnvironmentFile` override the unit's `Environment=`, whatever the order in
  the unit file. Such a line would point the new bot at the first bot's Claude
  account — the one failure this whole setup exists to prevent. The unit owns
  this variable.
- **`AGENT_BACKEND` other than `claude_sdk`.** The OpenCode path would need a
  second server on its own port with its own `OPENCODE_DB`; these units do not
  set that up.
- **A config directory with no Claude login.** The script prints the exact
  command: `CLAUDE_CONFIG_DIR=~/.claude-<name> claude auth login`. Skip this
  check with `--skip-auth-check` (it is skipped anyway when the instance `.env`
  sets `ANTHROPIC_API_KEY`).

It also warns, without stopping, when both instances share `BALAM_VNC_PORT` —
`/browser` then shows whichever agent's Chrome is on that display.

### What a new config directory starts with

`~/.claude-<name>` starts empty. The SDK backend asks for
`setting_sources=["user", …]` and `skills="all"`, so without help the new bot
sees none of your global skills and none of your settings allow-list, and every
tool call reaches the approval keyboard. The script therefore seeds the
directory: it **symlinks** `skills/` to `~/.claude/skills` (one copy, shared) and
**copies** `settings.json` and `CLAUDE.md` (independent from then on). Set
`BALAM_SEED_FROM` in `deploy.env` to seed from somewhere else, or pass
`--no-seed` to skip this. Plugins are not seeded; install them per config
directory if you want them.

### One-time tunnel setup (per instance)

Only if this instance needs a public Mini App:

```sh
cloudflared tunnel create balam-<name>
cloudflared tunnel route dns balam-<name> <your-host>
```

Then copy `cloudflared-balam.example.yml` to that instance's
`deploy/cloudflared-balam.yml`, point its ingress at
that instance's `BALAM_PORT`, register a BotFather Mini App for the new bot, and
put `BALAM_PUBLIC_URL` and `BALAM_MINIAPP_SHORTNAME` in that instance's
`deploy/balam.env`. Re-run the install script. Without a
tunnel the instance still works; `/diff` replies with a `127.0.0.1` URL instead
of an in-Telegram button.

### Operate

```sh
systemctl status balam@<name>
journalctl -u balam@<name> -f
sudo systemctl restart balam@<name>              # after editing that checkout or its .env
CLAUDE_CONFIG_DIR=~/.claude-<name> claude auth status
```

### What is shared, and what that means

Every instance runs as the same OS user. This is **configuration**
separation, not **security** separation: either bot's agent can read the other's
`.credentials.json`, files, SSH keys and `gh` login. That is acceptable when both
Claude accounts belong to one person, which is what this setup assumes. It is the
same trust boundary ADR-0008 already describes for
`ADDITIONAL_TELEGRAM_USER_IDS`.

If the two accounts belong to two different people, give the second instance its
own OS user instead. A different `HOME` gives it a different `~/.claude` with no
environment variable at all, and real file separation. The cost is that the
second user needs its own skills, its own git and `gh` credentials, and its own
uv environment.

## Alternative: quick (ephemeral) tunnel

For a throwaway test without DNS/BotFather, point `cloudflared-balam.service` at a
quick tunnel (`cloudflared tunnel --url http://127.0.0.1:3000 --config /dev/null`).
Its hostname changes on every restart, so run `deploy/refresh-tunnel-url.sh` after
each (re)start to rewrite `BALAM_PUBLIC_URL` in `balam.env` and restart the bot.
Note: a quick tunnel can't back a BotFather direct link (the URL must be stable),
so `/diff` in a group falls back to a browser URL button.
