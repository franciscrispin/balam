/**
 * Resolve how the Mini App was launched: which view to show and which workspace
 * context to act on.
 *
 * Two launch shapes, both supported:
 * - **URL query params** (`/?view=diff&context=balam`) — used when the app is
 *   opened in a plain browser or via a `web_app` button that carries a full URL.
 * - **Telegram `start_param`** — a direct Mini App link (`t.me/<bot>/<app>?startapp=…`,
 *   ADR-0013) can only pass this single token, so the bot encodes it as
 *   `"<view>__<context>"` (Telegram allows `[A-Za-z0-9_-]`). Query params win when
 *   present; otherwise we decode the start_param.
 */
import { resolveView, type ViewId } from "./views";

export interface Launch {
  view: ViewId;
  /** Workspace context to scope data to; undefined → backend default. */
  context: string | undefined;
}

export function resolveLaunch(startParam: string | undefined): Launch {
  const params = new URLSearchParams(window.location.search);
  let view = params.get("view") ?? undefined;
  let context = params.get("context") ?? undefined;

  // Decode the "view__context" start_param from a direct Mini App link, filling
  // only what the query string didn't already provide.
  if ((!view || !context) && startParam) {
    const [sv, sc] = startParam.split("__");
    view ??= sv || undefined;
    context ??= sc || undefined;
  }

  return { view: resolveView(view), context };
}
