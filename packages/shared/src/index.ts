/**
 * Types shared by the Balam backend and the Mini App.
 *
 * The Mini App API contract is generated from the backend's FastAPI OpenAPI
 * schema (ADR-0003) — `./api.ts` is produced by the root `gen:api` script and is
 * the single source of truth. This module re-exports the handful of generated
 * models the frontend consumes under stable names, so call sites import from
 * `@balam/shared` and never touch the generated `components[...]` shape directly.
 */
import type { components } from "./api";

export type { components, operations, paths } from "./api";

/** Identity of the running backend (`GET /api/app-info`). */
export type AppInfo = components["schemas"]["AppInfo"];

/** A single line within a diff hunk. `old_no`/`new_no` are null where absent. */
export type HunkLine = components["schemas"]["HunkLine"];

/** One contiguous hunk of a file's diff, pre-parsed + ready to highlight. */
export type DiffHunk = components["schemas"]["DiffHunk"];

/** The working-tree diff of a context (`GET /api/diff`). */
export type DiffResponse = components["schemas"]["DiffResponse"];
