/**
 * Resolve how the Mini App was launched: which view to show and which workspace
 * context to act on.
 *
 * Two launch shapes, both supported:
 * - **URL query params** (`/?view=diff&context=balam`) — used when the app is
 *   opened in a plain browser or via a `web_app` button that carries a full URL.
 * - **Telegram `start_param`** — a direct Mini App link (`t.me/<bot>/<app>?startapp=…`,
 *   ADR-0013) can only pass this single token, so the bot encodes it as
 *   `"<view>__<param>"` (Telegram allows `[A-Za-z0-9_-]`). Query params win when
 *   present; otherwise we decode the start_param.
 *
 * The second start_param token is a workspace context name (`diff__balam`)
 * unless it carries the `c_` content-id marker (`markdown__c_<hex>`), which
 * points the markdown view at an ephemeral snapshot served by
 * `/api/markdown/content/{id}`.
 */
import { resolveView, type ViewId } from "./views";

export interface Launch {
  view: ViewId;
  /** Workspace context to scope data to; undefined → backend default. */
  context: string | undefined;
  /** Ephemeral markdown snapshot id for the markdown view. */
  content: string | undefined;
}

const CONTENT_ID_RE = /^c_([0-9a-f]{6,})$/;

export function resolveLaunch(startParam: string | undefined): Launch {
  const params = new URLSearchParams(window.location.search);
  let view = params.get("view") ?? undefined;
  let context = params.get("context") ?? undefined;
  let content = params.get("content") ?? undefined;

  // Decode the "view__param" start_param from a direct Mini App link, filling
  // only what the query string didn't already provide.
  if ((!view || (!context && !content)) && startParam) {
    const [sv, sp] = startParam.split("__");
    view ??= sv || undefined;
    const contentId = sp?.match(CONTENT_ID_RE)?.[1];
    if (contentId) {
      content ??= contentId;
    } else {
      context ??= sp || undefined;
    }
  }

  return { view: resolveView(view), context, content };
}
