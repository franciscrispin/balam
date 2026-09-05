# Deploy — Balam under systemd + Cloudflare tunnel (ADR-0013)

Runs the Balam services under systemd (two, or three with the OpenCode backend)
and exposes **only** the Mini App to the internet through a Cloudflare **named
tunnel** (stable hostname), with Telegram `initData` (ADR-0008) as the trust
boundary. Read **ADR-0013** first — it states the security conditions this setup
enforces. Setting up a fresh VM? Work through the *Prerequisites* and *One-time
setup* sections below in order — several of those steps fail silently on the
Telegram or Cloudflare side. The installer and the bot catch what they can, and
each section says which check exists.

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

## Prerequisites

Install these on the VM before anything below. `install.sh` checks only for
`cloudflared`, and only when a tunnel is configured; anything else missing
fails later, somewhere less obvious.

- **`uv`, `bun`, `git`** — the same toolchain as local development (root
  `README.md`).
- **`cloudflared`** — for the named tunnel. Install the build for the VM's
  architecture ([GitHub releases](https://github.com/cloudflare/cloudflared/releases),
  or Cloudflare's [downloads page](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/downloads/)),
  then run `cloudflared tunnel login` once. It stores the account certificate in
  `~/.cloudflared/cert.pem`, which `tunnel create` and `tunnel route dns` need.
- **The agent runtime.** `AGENT_BACKEND=opencode` needs `opencode` on `PATH`.
  `AGENT_BACKEND=claude_sdk` needs a Claude login in the config directory the
  bot will use — `claude auth login`, or `CLAUDE_CONFIG_DIR=<dir> claude auth
  login` when `BALAM_PRIMARY_CLAUDE_CONFIG_DIR` pins one — or `ANTHROPIC_API_KEY`
  in `.env`.
- **A Telegram bot and a forum supergroup**, set up as the next section
  describes. The bot token, your user id and the group's chat id go in `.env`.

## One-time setup (creates public / account state — not in `install.sh`)

### Telegram: the bot and the group

Two settings here fail **silently** on Telegram's side: the bot looks alive,
answers `/status`, and ignores every plain message in a topic. Balam catches
what it can at boot. A chat id that is not a `-100…` supergroup id is a config
error, and privacy mode left on, Topics switched off, or a chat the bot cannot
see each log a `WARNING` in `journalctl -u balam` right after
`Application started`. Read that journal once after the first start.

1. **Turn group privacy mode off.** BotFather's default ("Enable") makes a bot
   in a group receive only commands, `@mentions` of it, and replies to its own
   messages — so plain messages in a forum topic never reach Balam. In
   BotFather: `/setprivacy` → pick the bot → **Disable**. Telegram applies the
   change to a group only when the bot is (re-)added, so remove the bot from the
   supergroup and add it back afterwards. (Promoting the bot to group **admin**
   also works: admins receive every message regardless of the setting.) Verify:
   ```sh
   curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe"
   # … "can_read_all_group_messages": true …
   ```
2. **Find `ALLOWED_TELEGRAM_CHAT_ID` (and your user id).** Create the group as
   a supergroup with **Topics** enabled *first*, add the bot, and only then read
   the id. A supergroup id starts with `-100`. Turning Topics on for an existing
   basic group upgrades it to a supergroup and gives it a **new** id: the old,
   shorter `-…` id still looks valid and produces the same silent ignore as
   privacy mode. To read the id, stop the bot (a running poller consumes the
   updates, and a second `getUpdates` caller gets `409 Conflict`), send
   `/status` in any topic (a command arrives even while privacy mode is still
   on), then:
   ```sh
   curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates" \
     | jq '.result[] | .message? // empty
            | {chat_id: .chat.id, type: .chat.type, is_forum: .chat.is_forum,
               title: .chat.title, from_id: .from.id, migrate_to: .migrate_to_chat_id}'
   ```
   Take the `-100…` `chat_id` whose `type` is `supergroup` and `is_forum` is
   `true`; `from_id` is your own `ALLOWED_TELEGRAM_USER_ID`. An update carrying
   `migrate_to_chat_id` is the upgrade itself, and that value is the id to use.
   Balam refuses to boot with an id of any other shape.

### Public Mini App: tunnel, BotFather app, overlay

1. **Named tunnel + DNS** (stable hostname). Tunnel names are shared by every
   machine on the Cloudflare account. `install.sh` asks Cloudflare who is
   connected to the tunnel before starting it and refuses when another machine
   is (`--allow-shared-tunnel` overrides), but it cannot pick a name for you,
   so look before creating one:
   ```sh
   cloudflared tunnel list     # CONNECTIONS shows where each tunnel is running now
   ```
   If `balam` is already listed, another machine is serving it. Pick a new name
   (say `balam-<host>`), set `BALAM_TUNNEL_NAME=<name>` in `deploy/deploy.env`
   so the unit runs that tunnel, and use it in place of `balam` below.
   `cloudflared tunnel create` refuses a duplicate name, but a copied
   credentials file or `cloudflared tunnel token <name>` joins the existing
   tunnel as a second connector: Cloudflare then spreads the Mini App's
   requests across both machines, and neither machine reports it.
   `cloudflared --config /dev/null tunnel info <name>` lists a tunnel's
   connectors; the `--config /dev/null` stops cloudflared from picking up an
   unrelated `~/.cloudflared/config.yml`, which otherwise makes it resolve the
   wrong tunnel.
   ```sh
   cloudflared tunnel create balam
   cloudflared tunnel route dns balam <your-host>      # e.g. balam.example.com
   ```
   Copy `cloudflared-balam.example.yml` to `cloudflared-balam.yml` (git-ignored) and
   fill in the tunnel id + hostname (ingress → `127.0.0.1:$BALAM_PORT`).
2. **BotFather Mini App — `/newapp`, not "Configure Mini App".** Send `/newapp`
   to BotFather, pick the bot, and answer its prompts: title, description, a
   640×360 photo, an optional demo GIF (`/empty` skips it), the Web App URL
   `https://<your-host>/`, and a **short name** (e.g. `diff`). The short name is
   what Balam's links need: `/diff` sends `t.me/<bot>/<shortname>?startapp=…`
   (`miniapp.py`), and only a `/newapp` app answers that form. BotFather's
   newer Bot Settings → **Configure Mini App** looks like the same step and is
   not: it sets the bot's *main* Mini App (`has_main_web_app`), which has no
   short name and a different link form. Without a short name, `/diff` in the
   supergroup falls back to a plain URL button that opens in the external
   browser, where there is no signed `initData`, so every `/api` call is
   rejected. `/myapps` in BotFather lists the apps that count.
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

No `deploy/cloudflared-balam.yml` means no tunnel: the script installs the bot
alone, `/diff` replies with a `127.0.0.1` URL, and `balam.env` is optional.
Create the ingress file and `balam.env` later and re-run to add the tunnel.
With an ingress file, `balam.env` is required, because a tunnel the bot does
not know the URL of is a quiet half-install. Before starting the tunnel the
script checks that `cloudflared` is installed and that no other machine is
already serving the same tunnel name (see the tunnel step above).

## Operate

```sh
systemctl status balam cloudflared-balam   # add balam-opencode in opencode mode
journalctl -u balam -f                     # bot + Mini App logs
journalctl -u cloudflared-balam -f         # tunnel logs
sudo systemctl restart balam               # after editing code / .env / balam.env
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
