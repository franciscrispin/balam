/**
 * Typed client for the Balam Mini App API (ADR-0003).
 *
 * Requests go to same-origin `/api/*` — served by the FastAPI backend in
 * production, and Vite-proxied to it in dev (vite.config.ts). The Telegram
 * webview's signed `initData` is forwarded as `Authorization: tma <initData>`
 * (ADR-0008). The API always requires valid `initData` — there is no auth bypass
 * (ADR-0013), so it answers only requests made from inside Telegram's webview;
 * a plain browser has no `initData` and is rejected with 401.
 */
import type { DiffResponse } from "@balam/shared";

function getInitData(): string {
  return window.Telegram?.WebApp?.initData ?? "";
}

/** A failed API response. `isAuth` marks the unrecoverable 401/403 cases. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }

  get isAuth(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

async function apiFetch<T>(path: string): Promise<T> {
  const initData = getInitData();
  // Only send the header when we actually have initData: an empty `tma ` is
  // malformed (401). Outside Telegram's webview there is none, so the request is
  // rejected — the API has no unauthenticated path (ADR-0013).
  const headers: HeadersInit = initData ? { Authorization: `tma ${initData}` } : {};

  const res = await fetch(`/api${path}`, { headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // non-JSON error body — keep the status text
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

/** Fetch the working-tree diff for a context (defaults to the server default). */
export function getDiff(context: string | undefined): Promise<DiffResponse> {
  const query = context ? `?context=${encodeURIComponent(context)}` : "";
  return apiFetch<DiffResponse>(`/diff${query}`);
}
