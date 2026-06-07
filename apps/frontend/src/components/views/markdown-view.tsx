import rehypeShikiFromHighlighter from "@shikijs/rehype/core";
import { useEffect, useState } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { HighlighterCore } from "shiki/core";
import { LoadingState } from "@/components/states/loading-state";
import { getHighlighterWith, HIGHLIGHT_THEME } from "@/lib/shiki";

// Languages used by the sample below; loaded before the sync rehype pass.
const MARKDOWN_LANGS = ["typescript"];

// Sample GFM — proves the react-markdown + remark-gfm + Shiki stack on-paper.
const SAMPLE = `# Markdown viewer

Balam renders agent output as prose. Links are [clay](https://claude.ai) and
inline code like \`send_message_draft\` sits on a soft \`--muted\` chip.

## Features

- GitHub-flavored markdown via **remark-gfm**
- On-paper syntax highlighting via **Shiki** (light theme)
- Tables, blockquotes, and task lists

> A blockquote reads with a clay-kraft left border on a muted ground.

\`\`\`typescript
export function draft(text: string): string | null {
  const trimmed = text.trim();
  return trimmed ? trimmed.slice(0, MAX_LEN) : null;
}
\`\`\`

| Surface  | Renderer       |
| -------- | -------------- |
| Diff     | Shiki (hand)   |
| Markdown | react-markdown |
`;

// Prose element styling keyed to the design tokens (design-system §6.3). Code
// blocks are left to Shiki (it owns the <pre> background); we only add the
// chrome (radius/border/padding) via descendant utilities on the wrapper.
const components: Components = {
  h1: ({ children }) => (
    <h1 className="mt-8 border-b border-border pb-2 font-serif text-[1.55rem] leading-tight font-semibold first:mt-0">
      {children}
    </h1>
  ),
  h2: ({ children }) => <h2 className="mt-8 font-sans text-[1.25rem] font-semibold">{children}</h2>,
  p: ({ children }) => <p className="mt-4 leading-[1.625]">{children}</p>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="text-primary underline-offset-2 hover:underline"
    >
      {children}
    </a>
  ),
  ul: ({ children }) => <ul className="mt-4 list-disc space-y-1 pl-6">{children}</ul>,
  ol: ({ children }) => <ol className="mt-4 list-decimal space-y-1 pl-6">{children}</ol>,
  blockquote: ({ children }) => (
    <blockquote className="mt-4 border-l-2 border-kraft bg-muted py-1 pl-4 text-muted-foreground">
      {children}
    </blockquote>
  ),
  table: ({ children }) => (
    <div className="mt-4 overflow-x-auto">
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-border bg-muted px-3 py-1.5 text-left font-semibold">
      {children}
    </th>
  ),
  td: ({ children }) => <td className="border border-border px-3 py-1.5">{children}</td>,
};

export default function MarkdownView() {
  const [highlighter, setHighlighter] = useState<HighlighterCore | null>(null);

  useEffect(() => {
    let cancelled = false;
    getHighlighterWith(MARKDOWN_LANGS).then((instance) => {
      if (!cancelled) setHighlighter(instance);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!highlighter) {
    return <LoadingState />;
  }

  return (
    <div className="mx-auto max-w-[68ch] [&_:not(pre)>code]:rounded [&_:not(pre)>code]:bg-muted [&_:not(pre)>code]:px-1.5 [&_:not(pre)>code]:py-0.5 [&_:not(pre)>code]:font-mono [&_:not(pre)>code]:text-[0.85em] [&_pre]:mt-4 [&_pre]:overflow-x-auto [&_pre]:rounded-lg [&_pre]:border [&_pre]:p-3 [&_pre]:text-[0.84rem]">
      <Markdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[
          [
            rehypeShikiFromHighlighter,
            highlighter,
            { theme: HIGHLIGHT_THEME, fallbackLanguage: "text" },
          ],
        ]}
        components={components}
      >
        {SAMPLE}
      </Markdown>
    </div>
  );
}
