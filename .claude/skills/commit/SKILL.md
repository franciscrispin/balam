---
name: commit
description: >
  Stage and commit changes using Conventional Commits format.
  Use when the user asks to commit, create a commit, or says /commit.
---

# Commit

## Process

1. Run `git status` (no `-uall`), `git diff` (staged + unstaged), and `git log --oneline -10` in parallel.
2. Group changes into **atomic commits** — each commit should represent one logical unit of work (see below).
3. For each group, in order:
   a. Stage only the files belonging to that group by name (never `git add -A` or `git add .`).
   b. Draft a commit message in Conventional Commits format for that group.
   c. Commit via HEREDOC.
4. Run `git status` after all commits to verify.

## Atomic Commits

Each commit must be a single, self-contained logical change. Group files by their shared purpose, not by proximity or file type.

**Grouping rules:**

- One feature, fix, refactor, or chore per commit.
- If a change spans multiple files but serves one purpose (e.g., a component + its styles + its test), those files belong in one commit.
- Separate unrelated changes even if they touch the same file — use `git add -p` to stage hunks individually when needed.
- Config/dependency changes (e.g., adding a package, updating `bun.lock`) go with the feature that needs them, unless they are independent.
- Documentation updates go in their own commit unless they are part of a feature being introduced in the same batch.

**When NOT to split:**

- If all changes serve a single logical purpose, make one commit — don't split artificially.
- If the user explicitly asks for a single commit, respect that.

## Conventional Commits Format

```
<type>(<optional scope>): <description>

[optional body]

[optional footer(s)]
```

**Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`

**Scopes:** This is a Bun-workspaces monorepo. Prefer a scope that names the affected workspace or area:

- `frontend` — `apps/frontend`
- `backend` — `apps/backend`
- `shared` — `packages/shared`
- A finer module name (e.g. `auth`, `api`) when the change is localized within a workspace.
- Omit the scope for repo-wide changes (tooling, root config, monorepo plumbing).

**Rules:**

- `<description>`: lowercase, imperative, no period, under 72 chars
- Body: explain _why_, not _what_ — wrap at 72 chars
- Breaking changes: add `!` after type/scope and `BREAKING CHANGE:` footer
- Keep the message concise — one sentence description is usually enough

**Examples:**

```
feat(frontend): add OAuth2 login flow
fix(backend): prevent race condition in queue processing
refactor(shared): extract date helpers into utils module
chore: bump biome to 2.4
docs: update monorepo setup instructions
refactor!: drop support for Node 14

BREAKING CHANGE: minimum Node version is now 16
```

## Commit HEREDOC Template

```bash
git commit -m "$(cat <<'EOF'
<type>(<scope>): <description>

<optional body>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

## Important

- Never push unless explicitly asked.
- Do not commit files that may contain secrets (`.env`, credentials, etc.). Note `.env.example` is safe to commit.
- Biome is the linter/formatter (`bun run lint`, `bun run format`).
