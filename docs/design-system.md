# Balam Mini App — Design System

A lean design system for the Balam Telegram Mini App. It borrows Anthropic's
claude.ai aesthetic — warm paper, a single clay accent, a serif/grotesque pair —
and adapts it for the three Mini App surfaces: the **git diff viewer**, the
**markdown viewer**, and the **live noVNC Chrome view**.

> Scope: this is the visual contract for `apps/frontend`. It is intentionally
> small — tokens + a component inventory + setup. It is not a full brand book.

**Stack:** React 19 + Vite (existing) · **Tailwind v4** · **shadcn/ui** (copy-in
Radix primitives). Theme is fixed: the claude.ai **light** palette, always.
Telegram's `themeParams`/`colorScheme` are ignored — Balam looks the same in
every chat theme. (No dark mode; the tokens are structured so one could be added
later, but it is out of scope.)

---

## 1. Principles

1. **Paper, not chrome.** The canvas is warm off-white, like claude.ai. Content
   sits on paper; UI furniture (borders, shadows) is whisper-quiet. Let the
   diff and the prose be the loudest things on screen.
2. **One accent, used sparingly.** Clay (`#cc785c`) is the only brand color.
   It marks the primary action and the focus ring — nothing decorative. If
   everything is clay, nothing is.
3. **Calm typography does the work.** A serif display face + a humanist
   grotesque body carry the personality, so color and motion don't have to.
4. **Native to Telegram, loyal to the brand.** Respect the webview: honor safe
   areas, keep tap targets ≥ 44px — but keep _our_ fixed light palette, ignoring
   the chat theme, so Balam always looks like Balam.
5. **Motion is feedback, not flair.** Short, eased, purposeful (150–250ms).
   One thing moves at a time. Respect `prefers-reduced-motion`.

---

## 2. Color

Warm, low-saturation neutrals from Anthropic's palette, a single clay accent,
and semantic colors tuned to read on paper. All values are exposed as CSS
variables so components never hardcode hex.

### Brand anchors

| Token        | Hex       | Use                                   |
| ------------ | --------- | ------------------------------------- |
| Clay         | `#cc785c` | Primary accent (buttons, focus, links)|
| Clay-hover   | `#b8634a` | Hover/active for clay surfaces        |
| Kraft        | `#d4a27f` | Soft accent / secondary fills         |
| Manilla      | `#ebdbbc` | Warm highlight, callout backgrounds   |

### Neutrals & roles (light)

| Role            | Variable             | Hex        |
| --------------- | -------------------- | ---------- |
| Background      | `--background`       | `#faf9f5`  |
| Surface (cards) | `--card`             | `#ffffff`  |
| Surface-sunken  | `--muted`            | `#f0eee6`  |
| Text            | `--foreground`       | `#1a1a18`  |
| Text-muted      | `--muted-foreground` | `#6b6961`  |
| Border          | `--border`           | `#e7e4d8`  |
| Primary         | `--primary`          | `#cc785c`  |
| Primary-fg      | `--primary-foreground`| `#ffffff` |
| Ring (focus)    | `--ring`             | `#cc785c`  |

### Semantic (diff + status)

| Role            | Background | Foreground |
| --------------- | --------- | ---------- |
| Added (diff)    | `#e9f3ec` | `#1f7a3d`  |
| Removed (diff)  | `#fbeae9` | `#b3322a`  |
| Added gutter    | `#cdebd6` | —          |
| Removed gutter  | `#f6d4d1` | —          |
| Warning         | `#fff3df` | `#9a6a00`  |
| Info / link     | —         | `#cc785c` (clay) |

> Diff colors are deliberately desaturated to sit on paper rather than the
> stoplight green/red of a typical IDE — closer to a printed redline.

---

## 3. Typography

Free approximations of Anthropic's faces. All loaded from Google Fonts (or
self-hosted `woff2` for offline-first; see Setup).

| Role            | Family            | Anthropic ref      | Notes                              |
| --------------- | ----------------- | ------------------ | ---------------------------------- |
| Display / headings | **Source Serif 4** | Tiempos / Copernicus | Serif, for H1–H2 and empty states |
| UI / body       | **Hanken Grotesk**| Styrene B          | Humanist grotesque, the workhorse  |
| Mono / code / diff | **JetBrains Mono** | —              | Diffs, code blocks, inline code    |

```
--font-serif: "Source Serif 4", Georgia, serif;
--font-sans:  "Hanken Grotesk", ui-sans-serif, system-ui, sans-serif;
--font-mono:  "JetBrains Mono", ui-monospace, "SF Mono", monospace;
```

### Type scale (1.250 — major third, 16px base)

| Token   | Size / line-height | Family | Weight | Use                         |
| ------- | ------------------ | ------ | ------ | --------------------------- |
| `display`| 31 / 38           | serif  | 600    | View title, empty states    |
| `h1`    | 25 / 32            | serif  | 600    | Markdown H1                 |
| `h2`    | 20 / 28            | sans   | 600    | Section headers, Markdown H2|
| `body`  | 16 / 26            | sans   | 400    | Prose, default text         |
| `small` | 14 / 20            | sans   | 400    | Metadata, captions          |
| `code`  | 13.5 / 22         | mono   | 400    | Diffs, code, inline code    |
| `label` | 12 / 16 (+0.02em)  | sans   | 600    | Buttons, tabs, file paths   |

Body prose maxes at **68ch** for readability. Mono never wraps inside the diff
viewer (horizontal scroll instead).

---

## 4. Spacing, radius, elevation

**Spacing** — 4px base scale: `1=4 · 2=8 · 3=12 · 4=16 · 6=24 · 8=32 · 12=48`.
Default screen padding is `4` (16px); honor Telegram safe-area insets on top of
that.

**Radius** — soft, not pill. `--radius: 0.625rem` (10px). Cards/sheets `10px`,
buttons/inputs `8px`, chips/badges `6px`, code blocks `8px`.

**Elevation** — paper casts soft, warm, low shadows; never hard black.

```
--shadow-sm: 0 1px 2px rgba(40, 35, 25, 0.06);
--shadow-md: 0 4px 16px rgba(40, 35, 25, 0.08);
--shadow-lg: 0 12px 32px rgba(40, 35, 25, 0.12);
```

Borders do most of the separating; reserve `shadow-md`/`lg` for sheets, popovers,
and the floating noVNC toolbar.

---

## 5. Motion

| Token        | Value                              | Use                          |
| ------------ | ---------------------------------- | ---------------------------- |
| `--ease`     | `cubic-bezier(0.2, 0, 0, 1)`       | Default (decelerate)         |
| `dur-fast`   | `150ms`                            | Hover, press, focus ring     |
| `dur-base`   | `220ms`                            | Sheets, tabs, expand/collapse|
| `dur-slow`   | `320ms`                            | View transitions             |

Patterns: page content does a single 8px rise + fade on mount; diff hunks
expand with a height+opacity transition; the streaming/loading state is a quiet
pulsing clay dot, not a spinner. Wrap everything in
`@media (prefers-reduced-motion: reduce) { * { animation: none; transition: none } }`.

---

## 6. Component inventory

Base primitives come from **shadcn/ui** (Button, Tabs, Card, Sheet, Badge,
Tooltip, ScrollArea, Skeleton, Toggle). They inherit the tokens above with no
restyling. The three Mini-App-specific views below are the things worth speccing.

### 6.1 App shell

- Full-bleed `--background`. A slim sticky **TopBar** (48px) with the current
  view title (`label` style) on the left, a context/menu affordance on the right.
- Respect `viewport-stable-height` and safe-area insets; the bar uses
  `env(safe-area-inset-top)` padding.
- Always light; no theme class to toggle.

### 6.2 Diff viewer

The signature surface. Reads like a printed redline, not an IDE.

- **File header**: mono `label` path + `+N −M` badges (added/removed semantic
  colors), collapse chevron. Sticky while scrolling a long file.
- **Hunks**: unified view by default (split is a `Toggle` for wide screens).
  Line gutter in `--muted-foreground`; added/removed rows use the diff semantic
  bg + gutter colors; the changed glyph (`+`/`−`) sits in the gutter, not inline.
- **Code**: `--font-mono` at `code` scale, tab-size 2, no wrap, horizontal
  scroll per file. Syntax highlighting tinted to stay on-paper (muted, not neon)
  — e.g. Shiki with a custom theme keyed off our tokens.
- **Empty/loading**: `Skeleton` rows, then a serif empty state ("No changes
  yet.") if the diff is empty.

### 6.3 Markdown viewer

Renders agent output / docs. This is the "prose" surface.

- Body `body` style, `--font-sans`, measure capped at 68ch, centered with
  generous vertical rhythm (headings get `mt-8`, paragraphs `mt-4`).
- **Headings** H1 serif, H2 sans-bold, with subtle `--border` underline on H1.
- **Code blocks**: `--muted` background, `8px` radius, mono, copy button on
  hover (top-right, `Tooltip`).
- **Links**: clay, underline on hover only. **Blockquotes**: left clay-kraft
  border + `--muted` bg. **Tables**: `--border` grid, zebra `--muted` rows.
- **Callouts** (optional): Manilla background for notes/tips.

### 6.4 Live noVNC Chrome view

- The agent's Chrome rendered by the noVNC RFB client straight into the content
  area (ADR-0006 as amended — no iframe). The scaled canvas fills the container;
  **we do not style inside it**.
- A floating bottom toolbar (`shadow-lg`, `--card`, `10px` radius) with
  connection status (clay dot = live, muted = reconnecting) and a fit/refresh
  control.
- **Connecting** state: centered serif label + pulsing clay dot over
  `--background`. **Offline** (the VNC stack isn't running): "No live browser
  session." + Retry `Button`. **Disconnected**: muted state with a Retry
  `Button`.

### 6.5 Shared states

Every view defines: **loading** (Skeleton), **empty** (serif sentence, no
illustration), **error** (muted text + Retry button). No full-screen spinners.

---

## 7. Accessibility & Telegram fit

- Contrast: body text ≥ 7:1 on paper; clay-on-white passes AA for UI text and
  large text — for clay _buttons_ we use white text (`--primary-foreground`).
- Focus is always visible: 2px `--ring` offset ring on every interactive
  element (Telegram users navigate with touch _and_ external keyboards).
- Tap targets ≥ 44×44px. Honor `prefers-reduced-motion`.
- Validate Mini App `initData` server-side on every request (ADR-0008) — not a
  visual concern, but the shell must surface an auth-failed state (muted error,
  no Retry) when it fails.

---

## 8. Setup

### Install

```bash
# from repo root (Bun workspace)
bun add -D tailwindcss @tailwindcss/vite
bun add tailwind-merge clsx class-variance-authority lucide-react
# shadcn/ui — copies components into apps/frontend/src/components/ui
bunx shadcn@latest init
```

Add the Tailwind Vite plugin in `apps/frontend/vite.config.ts`:

```ts
import tailwindcss from "@tailwindcss/vite";
// plugins: [react(), tailwindcss()]
```

### Tokens — `apps/frontend/src/styles/theme.css`

Tailwind v4 keeps theme in CSS. Define the (single, light) palette as `:root`
variables, then map them in `@theme inline` so utilities like `bg-background`,
`text-primary`, `font-serif` resolve to the tokens above.

```css
@import "tailwindcss";

:root {
  --background: #faf9f5;
  --card: #ffffff;
  --muted: #f0eee6;
  --foreground: #1a1a18;
  --muted-foreground: #6b6961;
  --border: #e7e4d8;
  --primary: #cc785c;
  --primary-foreground: #ffffff;
  --ring: #cc785c;
  --radius: 0.625rem;
  --font-serif: "Source Serif 4", Georgia, serif;
  --font-sans: "Hanken Grotesk", ui-sans-serif, system-ui, sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, monospace;
}

@theme inline {
  --color-background: var(--background);
  --color-card: var(--card);
  --color-muted: var(--muted);
  --color-foreground: var(--foreground);
  --color-muted-foreground: var(--muted-foreground);
  --color-border: var(--border);
  --color-primary: var(--primary);
  --color-primary-foreground: var(--primary-foreground);
  --color-ring: var(--ring);
  --radius-lg: var(--radius);
  --font-serif: var(--font-serif);
  --font-sans: var(--font-sans);
  --font-mono: var(--font-mono);
}
```

### Fonts

Self-host `woff2` under `apps/frontend/public/fonts` (offline-first — the VM may
not have outbound access in the webview) and `@font-face` them, or for quick
iteration add Google Fonts `<link>`s to `index.html`:

```
Source Serif 4 (400,600) · Hanken Grotesk (400,500,600) · JetBrains Mono (400,500)
```

### Telegram init

No theme syncing — the palette is fixed light. Just signal readiness and pin the
webview chrome to our paper background so it doesn't flash the chat's color:

```ts
const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.setBackgroundColor("#faf9f5");
tg?.setHeaderColor("#faf9f5");
```

---

## 9. Quick reference

```
Palette   paper #faf9f5 · ink #1a1a18 · clay #cc785c · border #e7e4d8
Fonts     Source Serif 4 (display) · Hanken Grotesk (UI) · JetBrains Mono (code)
Radius    10px cards · 8px buttons · 6px chips
Motion    150ms hover · 220ms sheets · ease (0.2,0,0,1)
Rule      one accent, paper canvas, calm type, motion = feedback
```
