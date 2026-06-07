import type { DiffHunk } from "@balam/shared";
import { EmptyState } from "@/components/states/empty-state";
import { HunkCard } from "./hunk-card";

// Sample hunk — the real viewer will fetch structured hunks from the backend
// (deferred). This drives the real Shiki pipeline to prove the surface.
const SAMPLE_HUNK: DiffHunk = {
  id: "sample-1",
  file_path: "apps/backend/src/balam/streamer.ts",
  language: "typescript",
  is_binary: false,
  is_empty: false,
  hunk_header: "@@ -12,6 +12,8 @@",
  lines: [
    { type: "context", old_no: 12, new_no: 12, content: "export function draft(text: string) {" },
    { type: "context", old_no: 13, new_no: 13, content: "  const trimmed = text.trim();" },
    { type: "delete", old_no: 14, new_no: null, content: "  return trimmed;" },
    { type: "add", old_no: null, new_no: 14, content: "  if (!trimmed) return null;" },
    { type: "add", old_no: null, new_no: 15, content: "  return trimmed.slice(0, MAX_LEN);" },
    { type: "context", old_no: 15, new_no: 16, content: "}" },
  ],
};

export default function DiffView() {
  return (
    <div className="space-y-6">
      <HunkCard hunk={SAMPLE_HUNK} />
      <section className="rounded-lg border border-dashed">
        <EmptyState message="No changes yet." />
      </section>
    </div>
  );
}
