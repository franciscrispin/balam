# Headed browser for agents (Xvfb + noVNC)

Lets an agent on this Linux VM run a _headed_ browser (through Playwright, or
any other X11 app) and have the window visible to the user from a normal
browser — no per-session action on the user's machine, no X server on their
machine, no SSH `-X`.

## Architecture

```
  agent ─── DISPLAY=:99 ───▶  Xvfb :99  ◀── x11vnc :5900 (localhost) ◀── websockify :6081 + noVNC
                                                                                    ▲
                                                                     user's browser (port 6081 forwarded)
                                                                http://localhost:6081/vnc.html
```

Xvfb paints to a virtual framebuffer. x11vnc exports it as a VNC server on
`localhost` only. websockify + noVNC bridges VNC → WebSocket and serves the
noVNC client at `/vnc.html`. The user reaches it through the same
port-forward they already use for other dev ports.

## One-time setup

These steps were run on Ubuntu 24.04 (noble), user `ubuntu`. Re-run them on a
fresh VM.

### 1. Fix the apt mirror (only if your sources still point at `sg.archive.ubuntu.com`)

The SG archive mirror started returning 404 for `noble` Release files in
April 2026. Swap it for the canonical mirror:

```sh
sudo sed -i.bak \
  's|http://sg\.archive\.ubuntu\.com/ubuntu|http://archive.ubuntu.com/ubuntu|g' \
  /etc/apt/sources.list.d/ubuntu.sources
sudo apt update
```

### 2. Install the X stack (system, needs sudo)

```sh
sudo apt install -y xvfb x11vnc
```

- `xvfb` — virtual framebuffer X server
- `x11vnc` — exposes any running X display over VNC

### 3. Install websockify (userland)

Ubuntu 24.04 enforces PEP 668, so `pip3 install --user` fails. Use pipx:

```sh
pipx install websockify
# → installs to ~/.local/bin/websockify
```

### 4. Install noVNC (userland)

Static JS/HTML, just clone it:

```sh
git clone --depth 1 https://github.com/novnc/noVNC.git ~/.local/share/novnc
```

### 5. Helper scripts

Already in this directory:

- `ensure.sh` — start the stack only if it is not already running, and
  (re)write `<project>/.playwright/cli.config.json` so a browser opened
  afterward fills the current Xvfb screen (window size = framebuffer,
  page viewport tracks the window). Safe to call any time; this is the one
  agents should use. The config file is generated — gitignored, not edited
  by hand.
- `start.sh` — unconditional (re)start of Xvfb + x11vnc + websockify/noVNC
- `stop.sh` — tears them down
- `profile.sh <name> [url]` — open the browser with a named persistent profile
  (an on-disk `user-data-dir` under `~/.cache/browser-use/profiles/<name>`, so
  a login survives across runs). Opt-in — only used when the user asks to
  reuse a saved login.

## Daily use

### Start the stack (once per VM boot)

```sh
.claude/skills/browser-use/headed-browser/ensure.sh
```

`ensure.sh` checks whether Xvfb `:99` is up and only starts the stack if it
is not — so it never disturbs a browser that is mid-session. Use `start.sh`
directly only when you deliberately want a clean restart (it kills any
previous instance, including a running browser, and starts fresh). Either
prints the noVNC URL and the `DISPLAY=:99` prefix to use.

### View it from your browser

Forward VM port **6081** the same way you forward other dev ports (SSH `-L`,
VS Code remote, whatever you use), then open:

```
http://localhost:6081/vnc.html
```

Click **Connect** (no password). You will see whatever is running on
`DISPLAY=:99`.

### Run a headed browser

```sh
DISPLAY=:99 playwright-cli open --headed https://example.com
DISPLAY=:99 playwright-cli click e5
DISPLAY=:99 playwright-cli close
```

The browser window appears in the noVNC tab. The agent can drive it without
any action on the user's machine.

### Stop the stack

```sh
.claude/skills/browser-use/headed-browser/stop.sh
```

## Configuration

Environment variables read by `start.sh` (all optional, with defaults):

| Var           | Default | Purpose                           |
| ------------- | ------- | --------------------------------- |
| `DISPLAY_NUM` | `99`    | X display number                  |
| `VNC_PORT`    | `5900`  | x11vnc RFB port (localhost-bound) |
| `NOVNC_PORT`  | `6081`  | websockify/noVNC HTTP port        |
| `WIDTH`       | `1440`  | Framebuffer width                 |
| `HEIGHT`      | `900`   | Framebuffer height                |
| `DEPTH`       | `24`    | Color depth                       |

Example — bigger screen:

```sh
WIDTH=1920 HEIGHT=1080 .claude/skills/browser-use/headed-browser/start.sh
.claude/skills/browser-use/headed-browser/ensure.sh   # rewrites cli.config.json to match the new size
```

The screen size is **not persisted** — a plain `start.sh` (e.g. after a
reboot, or `ensure.sh` starting the stack itself) goes back to 1440×900.
To make a different size the default, change `WIDTH`/`HEIGHT` here in
`start.sh`.

## Files and state

- `~/.cache/headed-browser/*.pid` — PIDs of running processes
- `~/.cache/headed-browser/{xvfb,x11vnc,websockify}.log` — per-process logs
- `/tmp/.X11-unix/X99` — Xvfb socket

## Security notes

- **x11vnc binds to `localhost` only** (`-localhost` flag). VNC auth is
  disabled (`-nopw`) because the port is not reachable from outside the VM.
- **websockify binds to `0.0.0.0:6081`** so the user can reach it through
  port forwarding. Anything else on the VM's network that can reach this VM
  on port 6081 can also view the noVNC session. If the VM is on an untrusted
  network, firewall 6081 or switch websockify to `--listen 127.0.0.1` and
  forward that.
- The scripts do not start on boot. Re-run `start.sh` after a reboot.

## SSH port-forward drops ("Broken pipe", "closed by remote host")

The `ssh -L 6081:...` forward is **only your viewport** into noVNC. The
browser, Xvfb, x11vnc and websockify all run on the VM and keep running when
the forward dies — so does whatever agent is driving them. You lose the
*picture*, not the work. Recover by re-running the forward and refreshing
`http://localhost:6081/vnc.html`.

To stop the forward from dropping (idle timeouts, flaky links):

```sh
# keepalives + bail out if forwarding fails, instead of a half-open tunnel
ssh -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
  -L 6081:localhost:6081 ubuntu@<vm-host>

# or, auto-reconnect forever (install autossh on the client):
autossh -M 0 -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L 6081:localhost:6081 ubuntu@<vm-host>
```

Or put it in `~/.ssh/config` once:

```
Host vm-vnc
    HostName <vm-host>
    User ubuntu
    LocalForward 6081 localhost:6081
    ServerAliveInterval 30
    ServerAliveCountMax 3
    ExitOnForwardFailure yes
```

then `ssh -N vm-vnc` (or `autossh -M 0 -N vm-vnc`).

If the agent's commands start failing with X-server errors after a
disconnect, the stack itself died (rare — `start.sh` now `nohup`s its
daemons). Just re-run `ensure.sh`; it restarts whatever is missing and
re-fits the window. And run long-lived things (the agent itself, ideally)
inside `tmux`/`screen` so an SSH drop never takes them down.

## Troubleshooting

- **`playwright-cli open --headed` fails with "Missing X server or $DISPLAY"**
  — the stack is not running, or `DISPLAY=:99` is not set. Run `ensure.sh` and
  prefix the command with `DISPLAY=:99`.
- **noVNC tab says "Failed to connect"** — websockify is not running, port
  6081 is not forwarded (the SSH tunnel dropped — see above), or x11vnc died.
  Check `~/.cache/headed-browser/*.log`; `ensure.sh` restarts what is missing.
- **Black screen in noVNC after connect** — no X client is running. That is
  expected until an agent launches a browser. Quick sanity check:
  `DISPLAY=:99 xeyes` (needs `x11-apps`) or a Playwright `open` command.
- **`apt install` returns 404** — see step 1. The SG mirror is the usual
  culprit.

## Why this, not the alternatives

- **SSH `-X` / X11 forwarding** — needs an X server on the user's machine and
  kills performance for Chromium.
- **CDP attach to a local Chrome** — works, but requires launching Chrome with
  `--remote-debugging-port` every session. Not autonomous.
- **Hosted (Browserbase / Steel)** — heavier, and they cannot reach the VM's
  `localhost` services.
- **Docker containers with noVNC baked in** — same stack but heavier; this
  setup runs directly on the VM host.
