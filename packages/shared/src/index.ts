/**
 * Types shared by the Balam backend and the Mini App.
 *
 * Real shared models (the Mini App API contract) will be generated from the
 * backend's FastAPI OpenAPI schema later (ADR-0003). For now this holds the
 * diff-hunk contract — mirroring the structured hunk format the backend will
 * emit for the diff viewer — plus a placeholder used to prove workspace wiring.
 */
export interface AppInfo {
  name: string;
  version: string;
}

/** A single line within a diff hunk. `old_no`/`new_no` are null where absent. */
export interface HunkLine {
  type: "context" | "add" | "delete";
  old_no: number | null;
  new_no: number | null;
  content: string;
}

/**
 * One contiguous hunk of a file's diff. The backend supplies these
 * pre-parsed; the frontend only renders + syntax-highlights them.
 */
export interface DiffHunk {
  id: string;
  file_path: string;
  /** Language id for syntax highlighting (e.g. "typescript", "python"). */
  language: string;
  is_binary: boolean;
  is_empty: boolean;
  /** The `@@ -a,b +c,d @@` header line. */
  hunk_header: string;
  lines: HunkLine[];
}
