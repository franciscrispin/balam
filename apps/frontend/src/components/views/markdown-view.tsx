import rehypeShikiFromHighlighter from "@shikijs/rehype/core";
import { useCallback, useEffect, useState } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { HighlighterCore } from "shiki/core";
import { useLaunchContext } from "@/components/launch-context";
import { EmptyState } from "@/components/states/empty-state";
import { ErrorState } from "@/components/states/error-state";
import { LoadingState } from "@/components/states/loading-state";
import { classifyApiError, getMarkdownContent } from "@/lib/api";
import { fenceLanguages, getHighlighterWith, HIGHLIGHT_THEME } from "@/lib/shiki";

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

type State =
  | { status: "loading" }
  | { status: "ready"; content: string; highlighter: HighlighterCore }
  | { status: "error"; message: string; recoverable: boolean };

export default function MarkdownView() {
  const { content: contentId } = useLaunchContext();
  const [state, setState] = useState<State>({ status: "loading" });

  // Returns a cancel cleanup so the effect drops a stale response on unmount;
  // Retry re-invokes it directly (same pattern as diff-view).
  const load = useCallback(() => {
    if (!contentId) return;
    let cancelled = false;
    setState({ status: "loading" });
    getMarkdownContent(contentId)
      // The rehype plugin highlights synchronously, so the grammars used by the
      // document's code fences must be loaded before the first render.
      .then(async (res) => ({
        content: res.content,
        highlighter: await getHighlighterWith(fenceLanguages(res.content)),
      }))
      .then(({ content, highlighter }) => {
        if (!cancelled) setState({ status: "ready", content, highlighter });
      })
      .catch((err) => {
        if (cancelled) return;
        setState({
          status: "error",
          ...classifyApiError(err, {
            fallback: "Couldn't load the document.",
            notFound: "This content has expired — ask the agent to send it again.",
          }),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [contentId]);

  useEffect(load, [load]);

  if (!contentId) {
    return <EmptyState message="Nothing to view — open this from a bot button." />;
  }
  if (state.status === "loading") {
    return <LoadingState />;
  }
  if (state.status === "error") {
    return <ErrorState message={state.message} onRetry={state.recoverable ? load : undefined} />;
  }
  return (
    <div className="mx-auto max-w-[68ch] [&_:not(pre)>code]:rounded [&_:not(pre)>code]:bg-muted [&_:not(pre)>code]:px-1.5 [&_:not(pre)>code]:py-0.5 [&_:not(pre)>code]:font-mono [&_:not(pre)>code]:text-[0.85em] [&_pre]:mt-4 [&_pre]:overflow-x-auto [&_pre]:rounded-lg [&_pre]:border [&_pre]:p-3 [&_pre]:text-[0.84rem]">
      <Markdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[
          [
            rehypeShikiFromHighlighter,
            state.highlighter,
            { theme: HIGHLIGHT_THEME, fallbackLanguage: "text" },
          ],
        ]}
        components={components}
      >
        {state.content}
      </Markdown>
    </div>
  );
}
