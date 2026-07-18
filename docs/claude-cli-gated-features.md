# Surfacing gated Claude Code features in Balam

The Claude Code CLI ships features — built-in tools, slash commands, bundled
skills — that exist in the installed binary but stay hidden in some sessions.
The visibility is decided by layered gates, and SDK sessions (which is what
Balam's `claude_sdk` backend spawns) are a *surface* the CLI deliberately
treats differently from the interactive terminal. So "the bot doesn't have X"
usually does **not** mean X is missing from the binary or the account; it
means a gate said no, and gates often have overrides.

This doc records the investigation method (it is repeatable whenever a new CLI
feature shows up interactively but not in the bot) and the artifacts case
study that produced it (2026-07-18).

## Where the truth lives

- **The installed binary**: `~/.local/share/claude/versions/<version>` — an
  ELF with the bundled, minified JS embedded. It is fully greppable and is the
  *authoritative* answer for "does this version have feature X and what gates
  it". The interactive session and every Balam SDK session run this same
  binary, so whatever you find applies to both.
- **`~/references/free-code`**: a reconstructed source snapshot of CLI
  **2.1.87**. Too old for anything recent, but invaluable for *mechanisms*
  because it is readable TypeScript: the bundled-skill registry
  (`src/skills/bundledSkills.ts`), command types (`src/types/command.ts`,
  `src/commands/`), compile-time `feature()` flags
  (`src/skills/bundled/index.ts`). Read the mechanism there, then find the
  current state in the binary.

## Mining the binary

Context regexes (`grep -oa '.\{0,150\}needle...'`) get OOM-killed on the
~260 MB binary. Use offsets + windows instead:

```sh
f=~/.local/share/claude/versions/<version>
grep -abo -F 'some-feature-name' "$f"          # byte offsets, fixed-string
dd if="$f" bs=1 skip=$((OFFSET-500)) count=1500 2>/dev/null \
  | tr -c '[:print:]\n' '.'                     # printable window around a hit
```

Tricks that made the artifacts investigation fast:

- **Find the export map.** Minified modules often carry
  `tt(X,{realName:()=>minifiedFn,...})` blocks. One window on such a block
  translated every opaque gate function into its real name
  (`isArtifactSdkDefaultOff`, `isArtifactHardDisabled`, …). Look for it before
  trying to reason about minified call chains.
- **Chase short minified names with anchored patterns** — `function B4(`,
  `name:e$e` — not bare `B4` (too many false hits).
- **Search for the gate vocabulary directly**: `tengu_<codename>` (statsig
  gates; features get internal codenames — artifacts is "cobalt plinth"),
  `CLAUDE_CODE_<X>` (env overrides), `enable<X>`/`disable<X>` (settings
  prefs), `CLAUDE_CODE_ENTRYPOINT` (surface checks).
- Bundled skills embed their **full SKILL.md text** in the binary. As a last
  resort a skill can be extracted verbatim and installed as a normal
  `~/.claude/skills/<name>/SKILL.md` — that sidesteps every gate, at the cost
  of never updating with the CLI. Only worth it for pure-guidance skills.

## Gate anatomy

A feature's visibility is typically the AND of:

1. **Compile-time `feature()` flags** — in or out of the build entirely.
2. **Auth/provider eligibility** — most claude.ai-backed features require
   first-party OAuth (`firstParty`); API-key/Bedrock/Vertex sessions are out.
   Balam runs on the owner's Max OAuth, so this passes.
3. **Server-side rollout** — a statsig gate (`tengu_*`) plus sometimes a
   subscription-tier check. Cannot be forced locally. Empirical test: if the
   feature shows up in an interactive `claude` on this VM, the account side
   passes.
4. **Surface checks** — `CLAUDE_CODE_ENTRYPOINT` is `sdk-py` for Balam
   sessions ( `sdk-ts`/`sdk-cli`/`mcp`/GitHub-Action are siblings). Features
   may be default-off for SDK surfaces **with a dedicated env override**
   (`CLAUDE_CODE_<FEATURE>=1`) — this is the gate Balam most often needs to
   flip, and it is the one that is safe to flip: it only expresses "this
   headless surface does want the feature".
5. **Settings prefs** — `enable<Feature>` / `disable<Feature>` in
   policy/flag/user settings scopes, usually defaulting to on.

### Command types decide what Balam can reuse

- `type: "prompt"` — skills (bundled or on disk). These flow through the SDK
  automatically once their `isEnabled` gate passes; Balam's slash-command
  passthrough already forwards them. Nothing to build.
- `type: "local"` / `"local-jsx"` — interactive UI (ink screens). These can
  **never** run over the SDK. But they are almost always thin UI over data
  that a built-in tool can also produce — find the tool action that backs the
  screen and give Balam its own command that prompts the agent to call it.

## Case study: the artifact stack (CLI 2.1.212, investigated 2026-07-18)

Symptom: interactive `claude` has `/artifacts`, the `Artifact` tool, and the
`artifact-design` / `artifact-capabilities` bundled skills; Balam sessions had
none of them. free-code (2.1.87) predates the whole feature.

Decoded chain (names from the binary's export map):

```
isArtifactToolEnabled =
      eligibility                # firstParty auth, not hard-disabled,
                                 #   not local-agent/claude-coworker surface,
                                 #   and NOT (SDK surface without opt-in)  ← the blocker
  AND rollout                    # statsig tengu_cobalt_plinth + pro/max/team/enterprise
  AND (enableArtifact pref ?? true)
```

- The blocker for Balam was `isArtifactSdkDefaultOff`: entrypoint
  `sdk-py` → default off. Its override is the env var
  **`CLAUDE_CODE_ARTIFACT=1`**, which skips only that check.
  (`CLAUDE_CODE_DISABLE_ARTIFACT` is the kill switch; managed settings can
  also hard-disable.)
- The `/artifacts` command is `local-jsx` ("Browse your published and shared
  artifacts") — interactive-only forever. The same data comes from the
  Artifact tool's `action:"list"` → `{artifacts:[{title,url,updatedAt,rel}],
  truncated, scope}` with scopes `mine`/`shared`/`all`.
- Permission wrinkle: the tool suppresses always-allow rules for `list` (and
  `live-edit`), so the first listing per session always asks — it lands on
  Balam's approval keyboard once per session. A `config.yaml` allow rule
  cannot pre-approve it, by design.

What Balam changed:

1. `claude_sdk_backend.py` `_build_options` sets `CLAUDE_CODE_ARTIFACT=1` in
   the SDK subprocess env. With that, the Artifact tool and the bundled
   artifact skills appear in every Balam session (account gates permitting) —
   the skills need no further wiring.
2. `bot.py` registers `/artifacts [shared|all]`, which submits a normal turn
   prompting the agent to call `Artifact(action:"list")` and format the
   result. On a backend without the tool (OpenCode, or an excluded account)
   the agent just reports the tool is unavailable.
3. `.env` sets `CLAUDE_SDK_CLI_PATH=/home/ubuntu/.local/bin/claude` — see the
   pitfall below; without it, steps 1–2 land in a binary that predates the
   feature and silently do nothing.

## Pitfall: the Python SDK spawns its own bundled CLI

The first live test failed even though the env var provably reached the
subprocess. Cause: `claude-agent-sdk` ships a **vendored CLI** at
`site-packages/claude_agent_sdk/_bundled/claude` and spawns *that* by default —
not the standalone install the binary investigation was done against. The
bundled copy lags the standalone channel (2.1.178 vs 2.1.21x at the time), and
a feature that postdates it simply is not there to enable.

- **Diagnose**: from inside a bot session, `ps -o args= -p $PPID` shows the
  exact binary running the session; `<path> --version` and
  `grep -c <feature-string> <path>` tell you if the feature exists in it.
- **Fix**: `CLAUDE_SDK_CLI_PATH` in `.env` (flows to
  `ClaudeAgentOptions.cli_path`) pointed at the launcher symlink
  `~/.local/bin/claude`, which tracks standalone CLI upgrades automatically.
  The alternative — waiting for a `claude-agent-sdk` release with a fresh
  bundle — leaves the bot's CLI version pinned to the SDK's release cadence.
- **Verify before restarting the bot**: a five-line `query()` probe with
  `cli_path` + the env override, reading the init message's `tools` /
  `skills` lists (`/tmp/artifact_probe2.py` pattern). Note the first probe run
  can show the tool but not the gated skills while the statsig cache is cold;
  trust the second run.

The marketplace `project-artifact` plugin is unrelated — it is a status-page
generator *on top of* the Artifact tool, not one of the bundled skills.

## Checklist for the next hidden feature

1. Interactive `claude` has it, the bot doesn't? → binary investigation, not
   reimplementation.
2. `grep -abo -F '<feature-name>' <binary>` → `dd` windows → find the export
   map → name the gates.
3. Classify each gate: compile-time / auth / rollout / surface / settings.
   Only surface gates (and settings) are yours to flip; look for the
   `CLAUDE_CODE_<X>` env override.
4. Slash command involved? Check its `type`. `prompt` flows for free;
   `local`/`local-jsx` needs a Balam command backed by the underlying tool
   action.
5. Wire env overrides in `_build_options` (per-session, in code — not in the
   systemd unit, so it survives redeploys and is visible in tests).
6. **Check which binary the SDK actually spawns** (`ps -o args= -p $PPID`
   inside a session). If it is the vendored
   `claude_agent_sdk/_bundled/claude`, confirm the feature exists at that
   version or point `CLAUDE_SDK_CLI_PATH` at the standalone launcher.
7. Prove the change with a direct `query()` probe (init message `tools` /
   `skills`) *before* restarting the bot.
8. Restart the bot **only between turns**, then verify in a fresh topic.
