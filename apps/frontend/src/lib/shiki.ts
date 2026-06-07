import type { HunkLine } from "@balam/shared";
import { createHighlighterCore, type HighlighterCore, type LanguageRegistration } from "shiki/core";
import { createJavaScriptRegexEngine } from "shiki/engine/javascript";

/**
 * Shared, lazily-created Shiki highlighter for the diff + markdown views.
 *
 * Uses the fine-grained `shiki/core` bundle with the JavaScript regex engine
 * (no Oniguruma wasm). Languages are loaded on demand from a static loader map,
 * so the build only emits chunks for this curated set (not Shiki's full
 * ~300-language registry) AND a view only fetches the grammars it actually uses.
 * The theme is light (design-system §6.2 — on paper, not the neon IDE palette).
 */

export const HIGHLIGHT_THEME = "github-light";

type LangLoader = () => Promise<{ default: LanguageRegistration[] }>;

const LANG_LOADERS: Record<string, LangLoader> = {
  typescript: () => import("@shikijs/langs/typescript"),
  tsx: () => import("@shikijs/langs/tsx"),
  javascript: () => import("@shikijs/langs/javascript"),
  jsx: () => import("@shikijs/langs/jsx"),
  python: () => import("@shikijs/langs/python"),
  json: () => import("@shikijs/langs/json"),
  bash: () => import("@shikijs/langs/bash"),
  yaml: () => import("@shikijs/langs/yaml"),
  toml: () => import("@shikijs/langs/toml"),
  css: () => import("@shikijs/langs/css"),
  html: () => import("@shikijs/langs/html"),
  markdown: () => import("@shikijs/langs/markdown"),
  sql: () => import("@shikijs/langs/sql"),
  go: () => import("@shikijs/langs/go"),
  rust: () => import("@shikijs/langs/rust"),
};

const engine = createJavaScriptRegexEngine({ forgiving: true });

let highlighterPromise: Promise<HighlighterCore> | null = null;

function getHighlighter(): Promise<HighlighterCore> {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighterCore({
      engine,
      themes: [import("@shikijs/themes/github-light")],
      langs: [],
    });
  }
  return highlighterPromise;
}

/** Load `lang`'s grammar if supported + not yet loaded; returns the resolved
 * language id ("text" when unsupported). */
async function ensureLanguage(highlighter: HighlighterCore, lang: string): Promise<string> {
  const loader = LANG_LOADERS[lang];
  if (!loader) {
    return "text";
  }
  if (!highlighter.getLoadedLanguages().includes(lang)) {
    const mod = await loader();
    await highlighter.loadLanguage(...mod.default);
  }
  return lang;
}

/** Highlighter with `langs` preloaded — for the markdown view, whose rehype
 * plugin highlights synchronously and so needs grammars ready up front. */
export async function getHighlighterWith(langs: string[]): Promise<HighlighterCore> {
  const highlighter = await getHighlighter();
  await Promise.all(langs.map((lang) => ensureLanguage(highlighter, lang)));
  return highlighter;
}

export interface HighlightedToken {
  content: string;
  /** Inline color from the theme; undefined for unstyled tokens. */
  color: string | undefined;
  /** Column offset within the line — a stable React key. */
  offset: number;
}

export interface HighlightedLine {
  tokens: HighlightedToken[];
  type: HunkLine["type"];
  old_no: number | null;
  new_no: number | null;
}

/** Tokenize hunk lines, preserving line numbers + change type. Rendered as
 * React spans by the caller (no raw HTML). Unknown languages fall back to
 * plain text. */
export async function highlightLines(lines: HunkLine[], lang: string): Promise<HighlightedLine[]> {
  const highlighter = await getHighlighter();
  const resolved = await ensureLanguage(highlighter, lang);

  const code = lines.map((line) => line.content).join("\n");
  const { tokens } = highlighter.codeToTokens(code, { lang: resolved, theme: HIGHLIGHT_THEME });

  return lines.map((line, index) => {
    const tokenLine = tokens[index] ?? [];
    return {
      tokens: tokenLine.map((token) => ({
        content: token.content,
        color: token.color,
        offset: token.offset,
      })),
      type: line.type,
      old_no: line.old_no,
      new_no: line.new_no,
    };
  });
}
