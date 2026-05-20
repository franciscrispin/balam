/**
 * Types shared by the Balam backend and the Mini App.
 *
 * For now this holds a single placeholder type so the workspace wiring is
 * proven end to end. Real shared models (diff hunks, file entries, the Mini App
 * API contract) will live here — see docs/architecture-decisions.md (ADR-0003).
 */
export interface AppInfo {
  name: string;
  version: string;
}
