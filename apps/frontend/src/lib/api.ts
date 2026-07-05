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
import type { BrowserStatus, DiffResponse, MarkdownContent } from "@balam/shared";

/**
 * The raw Telegram `initData`. Exported for the one consumer that can't ride
 * `apiFetch`: the noVNC WebSocket, where a browser can't set an Authorization
 * header and the backend instead expects `initData` as the first text frame
 * (ADR-0006).
 */
export function getInitData(): string {
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

/** The one auth-failure wording, shared by every view (incl. the noVNC one). */
export const AUTH_ERROR_MESSAGE = "Couldn't verify this Mini App session.";

/**
 * Map a failed request to error-state props. A 4xx won't change on an identical
 * retry (bad/absent session, unknown context, expired content), so it is not
 * recoverable — per design-system §7 only network failures and 5xx server
 * errors get a Retry. `notFound` overrides the 404 wording where the view has
 * something more helpful to say than the generic fallback.
 */
export function classifyApiError(
  err: unknown,
  messages: { fallback: string; notFound?: string },
): { message: string; recoverable: boolean } {
  const apiErr = err instanceof ApiError ? err : null;
  const message = apiErr?.isAuth
    ? AUTH_ERROR_MESSAGE
    : apiErr?.status === 404 && messages.notFound
      ? messages.notFound
      : messages.fallback;
  return { message, recoverable: !apiErr || apiErr.status >= 500 };
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

/** Fetch an ephemeral markdown snapshot (a plan, a sent .md file) by id. */
export function getMarkdownContent(id: string): Promise<MarkdownContent> {
  return apiFetch<MarkdownContent>(`/markdown/content/${encodeURIComponent(id)}`);
}

/** Whether the live browser stack (x11vnc) is reachable on the VM (ADR-0006). */
export function getBrowserStatus(): Promise<BrowserStatus> {
  return apiFetch<BrowserStatus>("/browser/status");
}
