---
name: browser-use
description: Drive a real, headed browser on this VM to autonomously test the Balam Telegram bot end-to-end — open Telegram Web logged in as the owner, send a message to the bot in a topic, and verify the full round-trip (typing indicator → animated streaming draft → final reply from the OpenCode agent). Use whenever the user asks to "test the bot", "verify the bot works", "drive the Telegram bot", "check the round-trip", "reproduce a bot bug in the browser", or otherwise exercise Balam through its real Telegram UI. The same stack also drives any other browser task (open a page, click, type, screenshot, read console/network) since it is built on a general headed Chromium + noVNC stack.
---

# Browser Use — autonomously test the Balam Telegram bot

Balam is a Telegram bot backed by an OpenCode agent (see `CLAUDE.md` and
`docs/architecture-decisions.md`). There is no API you can poke to prove the bot
works — the only true end-to-end test is **acting as the owner in a real
Telegram client** and watching the reply stream back. This skill does exactly
that: it drives a **real, visible browser** on this VM (you can watch it live in
a noVNC tab) through `playwright-cli`, opens Telegram Web logged in as the
owner, sends a message to the bot, and verifies the streamed round-trip.

The browser stack is general purpose, so the same commands drive any other web
task too. But the **headline workflow here is testing Balam** — start the two
backing processes, send a message, confirm the agent answers.

---

## How it fits together

```
  you ── playwright-cli ──▶ headed Chromium ──▶ web.telegram.org ──▶ Telegram ──▶ @your_bot
                              │ DISPLAY=:99                                          │
                              ▼                                                      ▼
                     Xvfb ─ x11vnc ─ noVNC                          Balam backend (apps/backend, Bun)
                              ▲                                                      │ HTTP + SSE
                   user watches at                                          OpenCode server (opencode serve)
              http://localhost:6081/vnc.html
```

You send a message in Telegram Web → Telegram delivers it to the bot → the Balam
backend routes the topic to an OpenCode session and prompts the agent → the
reply streams back into the same topic as an animated draft, then a final
message. **The test passes when you see that reply appear.**

- The headed-browser stack (Xvfb on `:99`, x11vnc, websockify/noVNC on `:6081`)
  is bundled in `headed-browser/`. `headed-browser/ensure.sh` starts it if it is
  not already up (idempotent, prompt-free when allowlisted);
  `headed-browser/README.md` has one-time install and troubleshooting.
- `playwright-cli` (the `@playwright/cli` package) drives the browser — a thin
  terminal front end over the Playwright MCP commands (`goto`, `click`, `fill`,
  `snapshot`, `eval`, `screenshot`, `console`, `network`, …). It is installed
  globally on the default Node (`v24.14.0`), so it is already on `PATH`.
- `DISPLAY=:99` is set for the whole project via the `env` block in
  `.claude/settings.json`, so every command — including `playwright-cli` —
  targets the Xvfb display the user can see. **Do not** `export DISPLAY=:99` as
  its own step. If you ever hit "Missing X server or $DISPLAY", prefix that one
  command with `DISPLAY=:99`.

---

## Prerequisites — bring the system up first

Testing the bot needs **two backing processes** running, plus the browser stack
and a logged-in Telegram profile. Confirm each before you send a test message;
the round-trip silently does nothing if any is missing.

### 1. The OpenCode server (the agent)

Balam is a *client* of a separate OpenCode server (ADR-0001/0002/0007) — it is
**not** in this repo and is **not** started by `bun run dev`. Start it yourself
if it is not already running. It must use the **same** password as
`OPENCODE_SERVER_PASSWORD` in `.env`:

```sh
OPENCODE_SERVER_PASSWORD=<the .env value> opencode serve --hostname 127.0.0.1 --port 4096
```

Run it in the background (a separate `tmux`/`screen` window, or
`run_in_background`) — it stays up for the whole session. Verify it answers:

```sh
curl -s -o /dev/null -w '%{http_code}\n' -u opencode:<password> http://127.0.0.1:4096/doc   # expect 200
curl -s -o /dev/null -w '%{http_code}\n'                        http://127.0.0.1:4096/doc   # expect 401 (auth required)
```

A `401` without auth and `200` with `-u opencode:<password>` is the healthy
state — the same Basic-auth handshake the backend's `opencode.py` performs. If
you get connection-refused, the server is not up. (`OPENCODE_BASE_URL` in `.env`
should match `http://127.0.0.1:4096`.) Don't read `.env` to fetch the secret —
ask the user for the password, or have them export it; the file is
read-protected on purpose.

### 2. The Balam backend (the bot)

The backend is **Python, managed by `uv`** (ADR-0011) — `bun run dev` only runs
the Mini App now, **not** the bot. With `.env` filled in at the repo root:

```sh
uv --directory apps/backend run balam     # or: cd apps/backend && uv run balam
```

(First run on a fresh checkout: `uv --directory apps/backend sync` to build the
venv.) It logs through Python's `logging` as `… [balam] INFO …`. The startup
sequence to watch for, in order:

- `[balam] INFO starting bot (owner <id>, workdir <dir>) ...`
- `[balam] INFO waiting for OpenCode at http://127.0.0.1:4096 ...`
- `[balam] INFO OpenCode is ready.` ← it cleared the OpenCode gate
- then python-telegram-bot's own `[telegram.ext.Application] INFO Application
  started` — the proof it is long-polling Telegram.

The backend waits for the OpenCode server (step 1) in its `post_init` hook
**before** polling, so start OpenCode **first**; if it is down the log stops at
`waiting for OpenCode …` and the bot never starts polling. Run `balam` in the
background and watch its log — each message you send produces streaming activity,
and any failure prints as `[balam.bot] ERROR failed to handle message` with a
traceback.

### 3. The headed-browser stack + Telegram login

```sh
.claude/skills/browser-use/headed-browser/ensure.sh
```

Idempotent — starts Xvfb/x11vnc/noVNC only if not already up, and writes
`.playwright/cli.config.json` so the browser fills the noVNC view. Tell the user
to watch at **http://localhost:6081/vnc.html** (port 6081 forwarded).

Telegram Web keeps its login in **IndexedDB**, so you must use a **persistent
profile** — a throwaway browser cannot stay logged in. Open it with:

```sh
.claude/skills/browser-use/headed-browser/profile.sh telegram https://web.telegram.org/a/
```

**The user logs in the first time** (QR scan or phone+code, in the noVNC window —
you never type their credentials). After that the `telegram` profile stays
logged in across runs. Full details and quirks: `references/telegram-web.md`.

> The Telegram account you log in as **must be the owner** whose numeric ID is
> `ALLOWED_TELEGRAM_USER_ID` in `.env`. The bot silently ignores everyone else
> (ADR-0008) — so if you test from any other account you will get *no reply* and
> wrongly conclude the bot is broken. Silence from a non-owner is correct
> behavior, not a bug.

---

## The test workflow

The end-to-end playbook — find the bot, send a message, verify the streamed
reply, read the logs — lives in **`references/balam-bot-testing.md`**. Read it
when you run a bot test; it has the exact selectors, what each phase looks like,
and how to tell a real failure from expected silence. The shape:

1. **Bring the system up** — the three prerequisites above.
2. **Open the bot's chat** in Telegram Web (the bot's DM, or the supergroup +
   topic Balam is wired to). Confirm the URL is `…/a/#<chatId>`.
3. **Send a test prompt** — type a message that makes the agent clearly *do*
   something verifiable (e.g. `what files are in this repo?` or `run pwd`), then
   `press Enter`.
4. **Watch the round-trip**, looking → waiting → looking:
   - a **typing** action / indicator appears,
   - an **animated draft** grows as the agent streams (the live preview),
   - then a **final message** with the agent's answer replaces it.
   Screenshot each stage for the user.
5. **Verify** the answer is plausibly the agent's (not a `⚠️` error), and
   cross-check the backend log shows the message was handled and the OpenCode
   session was prompted.
6. **Report** what you sent, what came back, timing, and any error — with
   screenshots.

Work in a tight loop: **look → act → look again.** Don't fire three actions and
hope. `playwright-cli snapshot` shows the accessibility tree with `ref=eNN`
handles; `playwright-cli eval '() => location.href' --raw` reads the URL to
confirm the right chat is open. After sending, **poll** the chat for the reply
(see "Waiting" in the cheatsheet) rather than `sleep`-ing a fixed time — the
agent's latency varies with the prompt.

### Capturing evidence

```sh
playwright-cli screenshot --filename sent.png        # the message you sent
playwright-cli screenshot --filename streaming.png   # the animated draft mid-stream
playwright-cli screenshot --filename reply.png       # the final reply
playwright-cli console error                          # JS errors on the page
playwright-cli network                                # failed Telegram requests (4xx/5xx)
```

Screenshot anything you report back. A stuck UI is very often a failed request
or an uncaught error visible in `console`/`network` in one line — and the *bot*
side surfaces in the `uv run balam` log, so check both.

### Wrap up

```sh
playwright-cli close            # close the browser when done
```

Leave the Xvfb/noVNC stack and the two backend processes running unless the user
wants them stopped — they are cheap and reused. `headed-browser/stop.sh` tears
the browser stack down; Ctrl-C (or kill) the `opencode serve` / `uv run balam`
background jobs to stop the bot.

---

## Things to keep in mind

- **Sending a message to the bot is a real, irreversible action**, but it is the
  whole point of this skill, so it does not need confirmation when the user
  asked you to test the bot. Do **not**, however, use the live browser to send
  messages to *other* people/chats, delete chats, or change account settings
  without asking.
- **Silence ≠ broken.** If you get no reply, walk the chain before concluding a
  bug: are you the **owner** account? Is `uv run balam` showing the message
  arrived? Is the OpenCode server up (`200` on `/doc`)? Did `console`/`network`
  show a Telegram error? The most common "bug" is testing from the wrong account
  or a backing process being down.
- **Snapshot refs go stale.** A `ref=eNN` is valid only in the snapshot that
  produced it. Telegram re-renders constantly; re-snapshot before reusing a ref,
  and prefer CSS/role selectors for anything you touch more than once.
- **Wait by polling, not sleeping.** `click`/`fill` already wait for the element.
  For "the reply appeared", poll with `eval --raw` in a short loop.
- **Watch for things outside the task.** A copy typo, a console error, a draft
  that never finalizes, a reply that arrives but is truncated at the 4096-char
  split — if you notice it, mention it.
- **Credentials.** This skill ships no stored Telegram login. The user logs in
  themselves in the noVNC window (persistent `telegram` profile). The OpenCode
  password comes from the user / their `.env`; do not read `.env` (it is
  deny-listed) or guess it.

---

## Bundled pieces

| Path                            | What it is                                                                 |
| ------------------------------- | -------------------------------------------------------------------------- |
| `headed-browser/ensure.sh`      | Start the stack if needed + write `.playwright/cli.config.json` — call this |
| `headed-browser/start.sh`       | Unconditionally (re)start Xvfb + x11vnc + websockify/noVNC                  |
| `headed-browser/stop.sh`        | Tear the stack down                                                        |
| `headed-browser/profile.sh`     | Open the browser with a named persistent profile (the `telegram` login)    |
| `headed-browser/README.md`      | One-time install, configuration, troubleshooting for the stack             |
| `references/balam-bot-testing.md` | The end-to-end bot-test playbook — read when testing the bot             |
| `references/telegram-web.md`    | Telegram Web quirks (login, navigation, file send) — read on demand        |

When you work out a new non-obvious flow (a different chat layout, a new failure
mode, a Mini App check), add it to `references/` rather than bloating this file.

---

## Prerequisites (one-time install)

- **Node** — `v24.14.0` is the default Node; `playwright-cli` is installed on it.
  If `playwright-cli` is missing: `npm install -g @playwright/cli@latest` then
  `playwright-cli install` (and `playwright-cli install-browser` if no browser
  is found).
- **Headed-browser stack** — `xvfb`, `x11vnc` (system, needs sudo);
  `websockify`, `noVNC` (userland). See `headed-browser/README.md`.
- **uv** — the Python backend runner (`uv run balam`). **Bun** — runs the Mini
  App (`bun run dev`). **OpenCode** — the `opencode` CLI for `opencode serve`.
  All already installed on this VM.

---

## `playwright-cli` cheatsheet

Full help: `playwright-cli --help` and `playwright-cli --help <command>`.
`DISPLAY=:99` is already in the environment, so none of the commands below need
an env prefix. Run each as a single command — do not chain them with
`&&`/`export`/`echo`, which forces a permission prompt.

### Navigating

```sh
playwright-cli open --headed https://example.com   # open a browser window
playwright-cli goto https://example.com/path       # navigate the open browser
playwright-cli go-back ; playwright-cli go-forward ; playwright-cli reload
playwright-cli resize 375 812                      # OPTIONAL: force an emulated viewport
playwright-cli tab-new <url> ; playwright-cli tab-list ; playwright-cli tab-select <i>
```

(For Telegram, open via `profile.sh telegram …`, not a throwaway `open` — the
login only persists in the profile.)

### Reading the page

```sh
playwright-cli snapshot               # accessibility tree, with ref=eNN handles
playwright-cli snapshot e123          # subtree rooted at a ref
playwright-cli eval '() => location.href' --raw
playwright-cli eval '() => document.title' --raw
playwright-cli console                # all console messages
playwright-cli console error          # only error level and above
playwright-cli network                # requests since the last navigation
```

The `ref=eNN` handles are stable **only inside the snapshot that produced
them**. After any DOM change, snapshot again before reusing a ref.

### Interacting

`click | dblclick | fill | type | hover | select | check | uncheck | press |
upload | drag` take either a `ref=eNN` from a fresh snapshot or a CSS /
Playwright selector. CSS selectors survive re-renders better.

```sh
playwright-cli fill 'div.input-message-input' 'what files are in this repo?'   # Telegram composer
playwright-cli press Enter                                                      # send it
playwright-cli click 'button:has-text("Submit")'
playwright-cli click 'role=button[name="Continue"]'
playwright-cli click e42                # ref from the latest snapshot
```

**Hover-revealed menus close between commands.** A menu that only appears while
the cursor is over its trigger is gone by your next `playwright-cli` command. Do
the hover **and** the click in one `run-code` snippet:

```sh
playwright-cli run-code 'async () => {
  await page.getByRole("button", { name: "<menu trigger>" }).hover();
  await page.waitForTimeout(300);
  await page.getByRole("menuitem", { name: "<item>", exact: true }).click();
}'
```

### Waiting (no first-class `wait-for`)

1. `click`/`fill` already wait for the element to be actionable.
2. For the reply to arrive or the URL to change, poll with `eval --raw`:

```sh
# wait until the right chat is open
for _ in $(seq 1 20); do
  u="$(playwright-cli eval '() => location.hash' --raw 2>/dev/null | tr -d '"')"
  [[ "$u" == *"#<chatId>"* ]] && break
  sleep 0.3
done
```

Avoid bare `sleep 5` — slow on average, flaky on the worst case. The agent's
reply can take seconds to tens of seconds; poll for the new message bubble
rather than guessing a fixed delay.

### Screenshots, video

```sh
playwright-cli screenshot --filename shot.png            # current viewport
playwright-cli screenshot --filename full.png --full-page
playwright-cli screenshot e42 --filename element.png     # one element
playwright-cli video-start session.webm ; playwright-cli video-stop
```

`screenshot` / `video-start` write relative to the current directory.

### Closing

```sh
playwright-cli close          # close the browser (keeps the Xvfb stack)
playwright-cli close-all      # close every browser session
playwright-cli kill-all       # force-kill stale/zombie browser processes
```

### Gotchas

- **Stale refs.** Re-snapshot after any DOM update before trusting a `ref=eNN`.
  Telegram re-renders the message list constantly.
- **`open` says a browser is already open.** Just `goto` the URL instead, or
  `playwright-cli close` first. For Telegram, if the profile is "in use" from a
  crashed run: `playwright-cli close-all` (or `kill-all`), then re-run
  `profile.sh telegram …`.
- **"Missing X server or $DISPLAY".** The headed stack is not running, or this
  shell did not inherit `DISPLAY`. Run
  `.claude/skills/browser-use/headed-browser/ensure.sh`, and if needed prefix the
  one failing command with `DISPLAY=:99`.
- **Stuck file-chooser modal** (from clicking the paperclip with nothing to
  upload) blocks `snapshot`/`eval`. Clear it with `playwright-cli upload <file>`
  or by reopening the browser — see `references/telegram-web.md`.
