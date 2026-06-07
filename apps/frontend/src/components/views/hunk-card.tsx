import type { DiffHunk } from "@balam/shared";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { type HighlightedLine, highlightLines } from "@/lib/shiki";
import { cn } from "@/lib/utils";

/**
 * Renders one diff hunk as a printed redline (design-system §6.2): mono file
 * header with +N −M badges, line-number gutters, the +/− glyph in the gutter
 * (not inline), and Shiki-highlighted content on a desaturated row.
 */
export function HunkCard({ hunk }: { hunk: DiffHunk }) {
  const [lines, setLines] = useState<HighlightedLine[] | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    highlightLines(hunk.lines, hunk.language).then((result) => {
      if (!cancelled) setLines(result);
    });
    return () => {
      cancelled = true;
    };
  }, [hunk]);

  const added = hunk.lines.filter((line) => line.type === "add").length;
  const removed = hunk.lines.filter((line) => line.type === "delete").length;

  return (
    <div className="overflow-hidden rounded-lg border bg-card shadow-sm">
      <button
        type="button"
        onClick={() => setCollapsed((value) => !value)}
        className="flex w-full items-center gap-2 border-b bg-muted/50 px-3 py-2 text-left"
      >
        {collapsed ? (
          <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronDown className="size-4 shrink-0 text-muted-foreground" />
        )}
        <span className="truncate font-mono text-[0.84rem] font-semibold tracking-[0.02em]">
          {hunk.file_path}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-1.5">
          <Badge className="bg-diff-added font-mono text-diff-added-fg">+{added}</Badge>
          <Badge className="bg-diff-removed font-mono text-diff-removed-fg">−{removed}</Badge>
        </span>
      </button>

      {!collapsed ? (
        <div className="overflow-x-auto">
          {lines === null ? (
            <pre className="px-3 py-2 font-mono text-[0.84rem] text-muted-foreground">
              Highlighting…
            </pre>
          ) : (
            <table className="w-full border-collapse font-mono text-[0.84rem] leading-[1.6]">
              <tbody>
                {lines.map((line) => {
                  const key = line.new_no !== null ? `n${line.new_no}` : `o${line.old_no}`;
                  return (
                    <tr
                      key={key}
                      className={cn(
                        line.type === "add" && "bg-diff-added",
                        line.type === "delete" && "bg-diff-removed",
                      )}
                    >
                      <td className="w-10 px-2 text-right tabular-nums text-muted-foreground select-none">
                        {line.old_no ?? ""}
                      </td>
                      <td className="w-10 px-2 text-right tabular-nums text-muted-foreground select-none">
                        {line.new_no ?? ""}
                      </td>
                      <td
                        className={cn(
                          "w-5 text-center select-none",
                          line.type === "add" && "text-diff-added-fg",
                          line.type === "delete" && "text-diff-removed-fg",
                        )}
                      >
                        {line.type === "add" ? "+" : line.type === "delete" ? "−" : ""}
                      </td>
                      <td className="pr-4 whitespace-pre">
                        {line.tokens.length > 0
                          ? line.tokens.map((token) => (
                              <span
                                key={token.offset}
                                style={token.color ? { color: token.color } : undefined}
                              >
                                {token.content}
                              </span>
                            ))
                          : " "}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      ) : null}
    </div>
  );
}
