#!/usr/bin/env bash
# Install an EXTRA Balam instance: a second bot, driven by a second Claude
# account, on this same VM and the same OS user.
#
#   deploy/install-instance.sh <name> [--no-seed] [--skip-auth-check] [--allow-shared-tunnel]
#
# The first instance stays as it is (balam.service, this checkout, its Claude
# config dir). Every instance after it follows this convention, which the unit
# template is rendered with — the paths come from the machine and from
# deploy/deploy.env, not from a hardcoded host:
#
#   checkout   <instance root>/<prefix>-<name>   own .env, config.yaml, balam.sqlite
#   Claude     <home>/.claude-<name>             own login, settings, skills, sessions
#   units      balam@<name>.service              + cloudflared-balam@<name>.service
#
# By default <instance root>/<prefix> is the parent directory and basename of THIS
# checkout, so the instance lands next to the primary.
#
# The second Claude account comes from CLAUDE_CONFIG_DIR, which the unit sets.
# The `claude` CLI resolves .credentials.json, .claude.json, projects/,
# settings.json, skills/ and plugins/ from that directory, and the Agent SDK
# spawns the CLI with the unit's environment, so no Balam code change is needed.
#
# Same OS user as the first instance, so this is CONFIG separation, not SECURITY
# separation: either instance's agent can read the other's credentials and files.
# That is the right trade only when both accounts belong to one person. For two
# different people, run the second instance as its own OS user instead — a
# different HOME gives it a different ~/.claude with no env var at all.
#
# Idempotent: re-run it after editing the instance .env or pulling new code.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
balam_deploy_init

PRIMARY="$BALAM_REPO"
USER_HOME="$BALAM_HOME"

usage() {
  cat <<'USAGE'
Install an extra Balam instance: a second bot on a second Claude account.

  deploy/install-instance.sh <name> [--no-seed] [--skip-auth-check] [--allow-shared-tunnel]

  <name>              instance name, e.g. "work". Its checkout sits next to this
                      one as <prefix>-<name>, and its Claude account lives in
                      ~/.claude-<name>. Both paths are printed below.
  --no-seed           do not copy ~/.claude/settings.json + CLAUDE.md or symlink
                      skills/ into the new config dir.
  --skip-auth-check   install even when that config dir has no Claude login yet.
  --allow-shared-tunnel
                      start this instance's tunnel (balam-<name>) even though
                      another machine is already serving it.

The header of this script explains the layout and the trust boundary.
USAGE
}

# --- arguments --------------------------------------------------------------

NAME=""
SEED=1
AUTH_CHECK=1
ALLOW_SHARED_TUNNEL=${BALAM_ALLOW_SHARED_TUNNEL:-0}
for arg in "$@"; do
  case "$arg" in
    --no-seed) SEED=0 ;;
    --skip-auth-check) AUTH_CHECK=0 ;;
    --allow-shared-tunnel) ALLOW_SHARED_TUNNEL=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*)
      echo "unknown option: $arg" >&2
      exit 2
      ;;
    *)
      if [ -n "$NAME" ]; then
        echo "give exactly one instance name (got '$NAME' and '$arg')" >&2
        exit 2
      fi
      NAME="$arg"
      ;;
  esac
done

if [ -z "$NAME" ]; then
  usage >&2
  exit 2
fi
if ! printf '%s' "$NAME" | grep -qE '^[a-z][a-z0-9-]{0,20}$'; then
  echo "instance name must be lowercase letters, digits and dashes, starting with a letter: '$NAME'" >&2
  exit 2
fi

REPO="$BALAM_INSTANCE_ROOT/$BALAM_INSTANCE_PREFIX-$NAME"
CFG="$USER_HOME/.claude-$NAME"
ENV_FILE="$REPO/.env"
OVERLAY="$REPO/deploy/balam.env"

# uv and bun are resolved by lib.sh (command -v, or a deploy.env override), so
# check the resolved paths rather than PATH — the installer may well run from a
# shell whose PATH differs from the unit's.
command -v git >/dev/null || {
  echo "git is not on PATH" >&2
  exit 2
}
for tool in "$BALAM_UV" "$BALAM_BUN"; do
  [ -x "$tool" ] || {
    echo "$tool is not executable — set BALAM_UV / BALAM_BUN in deploy/deploy.env" >&2
    exit 2
  }
done

# --- helpers ----------------------------------------------------------------

problems=()
problem() { problems+=("$1"); }
warn() { printf '  ! %s\n' "$1"; }


port_in_use() { ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"; }

claude_email() { # claude_email <config dir>  -> the account's email, or ""
  # Both `|| true`s matter under `set -o pipefail`: a config dir with no login
  # makes `claude auth status` and then grep fail, and an unguarded failure here
  # would abort the script instead of reporting "not logged in".
  local status
  status=$(CLAUDE_CONFIG_DIR="$1" claude auth status 2>/dev/null) || return 0
  printf '%s' "$status" | grep -o '"email": *"[^"]*"' | cut -d'"' -f4 || true
}

# --- the checkout -----------------------------------------------------------

if [ ! -d "$REPO" ]; then
  echo "==> Creating the instance checkout at $REPO"
  # Clone from the first checkout: fast, offline, and it cannot land on a
  # different revision than the one you are installing from. Only committed work
  # is copied. Origin is then repointed at the real remote so `git pull` in the
  # instance behaves normally.
  git clone --quiet "$PRIMARY" "$REPO"
  origin=$(git -C "$PRIMARY" remote get-url origin 2>/dev/null || true)
  if [ -n "$origin" ]; then
    git -C "$REPO" remote set-url origin "$origin"
    echo "    cloned from $PRIMARY; origin now $origin"
  fi
fi

# .env and config.yaml are git-ignored, so a fresh clone has neither. Seed them
# from the examples and stop: the values that must differ per instance are the
# ones only the operator knows.
if [ ! -f "$ENV_FILE" ]; then
  cp "$REPO/.env.example" "$ENV_FILE"
  problem "$ENV_FILE was missing — copied from .env.example. Fill in TELEGRAM_BOT_TOKEN (a second BotFather bot), ALLOWED_TELEGRAM_USER_ID, ALLOWED_TELEGRAM_CHAT_ID, AGENT_BACKEND=claude_sdk and a free BALAM_PORT."
fi
if [ ! -f "$REPO/config.yaml" ]; then
  cp "$REPO/config.example.yaml" "$REPO/config.yaml"
  problem "$REPO/config.yaml was missing — copied from config.example.yaml. Point its contexts at the directories this bot should work in."
fi

# --- checks that stop a quietly-broken instance -----------------------------

echo "==> Checking the instance configuration"

backend=$(balam_env_get "$ENV_FILE" AGENT_BACKEND)
if [ "$backend" != "claude_sdk" ]; then
  problem "AGENT_BACKEND is '${backend:-unset}'. Extra instances are claude_sdk-only: the whole point is a second Claude account, and the OpenCode path would need its own server port and its own OPENCODE_DB, which these units do not set up."
fi

token=$(balam_env_get "$ENV_FILE" TELEGRAM_BOT_TOKEN)
if [ -z "$token" ]; then
  problem "TELEGRAM_BOT_TOKEN is empty in $ENV_FILE. Make a second bot with BotFather."
elif [ "$token" = "$(balam_env_get "$PRIMARY/.env" TELEGRAM_BOT_TOKEN)" ]; then
  problem "TELEGRAM_BOT_TOKEN is the same token as $PRIMARY/.env. Two pollers on one token make Telegram return 409 Conflict, and both bots stop receiving messages. Each instance needs its own bot."
fi

primary_port=$(balam_env_get "$PRIMARY/.env" BALAM_PORT)
primary_port=${primary_port:-3000}
port=$(balam_env_get "$ENV_FILE" BALAM_PORT)
if [ -z "$port" ]; then
  problem "BALAM_PORT is unset in $ENV_FILE. It defaults to 3000, which the first instance owns — pick a free port (e.g. $((primary_port + 1)))."
elif [ "$port" = "$primary_port" ]; then
  problem "BALAM_PORT $port is the first instance's port. The Mini App server binds 127.0.0.1:BALAM_PORT and the port is single-owner."
elif ! systemctl is-active --quiet "balam@$NAME.service" && port_in_use "$port"; then
  problem "BALAM_PORT $port is already listening and it is not this instance (see: ss -ltn)."
fi

# The unit sets CLAUDE_CONFIG_DIR, and systemd applies EnvironmentFile *over*
# Environment= whatever the order in the unit file. A line in either env file
# would therefore win and run this instance as the first instance's Claude
# account — the exact failure this whole setup exists to avoid.
for f in "$ENV_FILE" "$OVERLAY"; do
  if balam_env_has "$f" CLAUDE_CONFIG_DIR; then
    problem "$f sets CLAUDE_CONFIG_DIR. systemd lets an EnvironmentFile override the unit's Environment=, so that line would silently point this instance at another account's Claude login. Remove it — balam@.service owns this variable and sets it to $CFG."
  fi
done

db=$(balam_env_get "$ENV_FILE" BALAM_DB_PATH)
# A relative BALAM_DB_PATH resolves against each unit's own WorkingDirectory, so
# the two databases are already separate. Only an absolute path can collide.
if [ -n "$db" ] && [ "${db#/}" != "$db" ] && [ "$db" = "$(balam_env_get "$PRIMARY/.env" BALAM_DB_PATH)" ]; then
  problem "BALAM_DB_PATH $db is the first instance's database. Sharing it would mix both bots' topic-to-session maps and schedules."
fi

# Tunnel names are account-wide (balam_tunnel_check in lib.sh has the story): a
# second machine on the same tunnel round-robins this instance's Mini App with
# it, and nothing on either machine says so.
if [ -f "$REPO/deploy/cloudflared-balam.yml" ]; then
  if ! BALAM_ALLOW_SHARED_TUNNEL="$ALLOW_SHARED_TUNNEL" balam_tunnel_check "balam-$NAME" "cloudflared-balam@$NAME.service"; then
    problem "cannot start tunnel balam-$NAME (details above). Use a tunnel of your own, stop it on the other machine, or pass --allow-shared-tunnel."
  fi
fi

if [ "${#problems[@]}" -gt 0 ]; then
  echo >&2
  echo "Cannot install instance '$NAME' yet:" >&2
  for p in "${problems[@]}"; do
    printf '  - %s\n' "$p" >&2
  done
  echo >&2
  echo "Fix these and re-run: deploy/install-instance.sh $NAME" >&2
  exit 1
fi

# Warnings: real, but not worth refusing to install over.
vnc=$(balam_env_get "$ENV_FILE" BALAM_VNC_PORT)
primary_vnc=$(balam_env_get "$PRIMARY/.env" BALAM_VNC_PORT)
if [ "${vnc:-5900}" = "${primary_vnc:-5900}" ]; then
  warn "BALAM_VNC_PORT ${vnc:-5900} is shared with the first instance, so /browser shows whichever agent's Chrome is on that display. Give this instance its own x11vnc port if both will browse."
fi
config_path=$(balam_env_get "$ENV_FILE" BALAM_CONFIG_PATH)
if [ -n "$config_path" ] && [ "${config_path#"$REPO"}" = "$config_path" ]; then
  warn "BALAM_CONFIG_PATH points outside $REPO ($config_path), so this bot's contexts are not the ones in its own checkout."
fi

# --- the Claude login for this instance -------------------------------------

mkdir -p "$CFG"

if [ "$AUTH_CHECK" -eq 1 ] && [ -z "$(balam_env_get "$ENV_FILE" ANTHROPIC_API_KEY)" ]; then
  email=$(claude_email "$CFG")
  if [ -z "$email" ]; then
    echo >&2
    echo "No Claude login in $CFG." >&2
    echo "Log the second account in, then re-run this script:" >&2
    echo >&2
    echo "    CLAUDE_CONFIG_DIR=$CFG claude auth login" >&2
    echo >&2
    echo "(Or pass --skip-auth-check to install anyway.)" >&2
    exit 1
  fi
  echo "    Claude account for this instance: $email"
  if [ "$email" = "$(claude_email "${BALAM_PRIMARY_CLAUDE_CONFIG_DIR:-$USER_HOME/.claude}")" ]; then
    warn "that is the same account the first instance uses — both bots will draw on one subscription."
  fi
fi

# --- build ------------------------------------------------------------------

echo "==> Syncing the backend venv (uv sync)"
(cd "$REPO/apps/backend" && "$BALAM_UV" sync --quiet)

echo "==> Building the Mini App (FastAPI serves dist/ from this checkout)"
(cd "$REPO" && "$BALAM_BUN" install --silent >/dev/null)
balam_build_miniapp "$REPO"

# --- seed the config dir ----------------------------------------------------

if [ "$SEED" -eq 1 ]; then
  echo "==> Seeding $CFG from $BALAM_SEED_FROM (nothing existing is overwritten)"
  # A fresh config dir has no skills and no settings, and the SDK backend asks
  # for setting_sources=["user",...] with skills="all" — so without this the
  # instance's agent sees none of the global skills and none of the settings
  # allow-list, and every tool call goes to the approval keyboard.
  if [ ! -e "$CFG/skills" ] && [ -d "$BALAM_SEED_FROM/skills" ]; then
    ln -s "$BALAM_SEED_FROM/skills" "$CFG/skills"
    echo "    skills -> $BALAM_SEED_FROM/skills (symlink: one copy, both instances)"
  fi
  for f in settings.json CLAUDE.md; do
    if [ ! -e "$CFG/$f" ] && [ -f "$BALAM_SEED_FROM/$f" ]; then
      cp "$BALAM_SEED_FROM/$f" "$CFG/$f"
      echo "    $f copied (independent from now on)"
    fi
  done
fi

# --- units ------------------------------------------------------------------

echo "==> Rendering unit files into /etc/systemd/system"
# Shared templates: every extra instance runs the same balam@.service, so this
# also updates any instance installed earlier from an older checkout. That is
# safe only because the template is machine-level, not instance-level — every
# per-instance value reaches it through systemd's %i.
balam_render "$SCRIPT_DIR/balam@.service.in" /etc/systemd/system/balam@.service

TUNNEL=0
if [ -f "$REPO/deploy/cloudflared-balam.yml" ]; then
  TUNNEL=1
  balam_render "$SCRIPT_DIR/cloudflared-balam@.service.in" /etc/systemd/system/cloudflared-balam@.service
  sudo mkdir -p /etc/cloudflared
  sudo install -m 0644 "$REPO/deploy/cloudflared-balam.yml" "/etc/cloudflared/balam-$NAME.yml"
  echo "    tunnel ingress -> /etc/cloudflared/balam-$NAME.yml"
else
  echo "    no $REPO/deploy/cloudflared-balam.yml — skipping the tunnel."
  echo "    The Mini App still works at http://127.0.0.1:$port; /diff replies with"
  echo "    that URL instead of an in-Telegram button (see deploy/README.md)."
fi

sudo systemctl daemon-reload

echo "==> Enabling + (re)starting instance '$NAME'"
sudo systemctl enable --quiet "balam@$NAME.service"
sudo systemctl restart "balam@$NAME.service"
if [ "$TUNNEL" -eq 1 ]; then
  sudo systemctl enable --quiet "cloudflared-balam@$NAME.service"
  sudo systemctl restart "cloudflared-balam@$NAME.service"
fi

echo
if [ "$TUNNEL" -eq 1 ]; then
  systemctl --no-pager --lines=0 status "balam@$NAME" "cloudflared-balam@$NAME" || true
else
  systemctl --no-pager --lines=0 status "balam@$NAME" || true
fi

cat <<EOF

Instance '$NAME' is installed.

  logs      journalctl -u balam@$NAME -f          ("Application started" = polling Telegram)
  restart   sudo systemctl restart balam@$NAME    (after editing code or $ENV_FILE)
  claude    CLAUDE_CONFIG_DIR=$CFG claude auth status
EOF
