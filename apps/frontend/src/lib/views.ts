/** The Mini App surfaces (ADR-0003). One app, one view at a time. */
export const VIEWS = ["diff", "markdown", "browser"] as const;

export type ViewId = (typeof VIEWS)[number];

export const VIEW_TITLES: Record<ViewId, string> = {
  diff: "Changes",
  markdown: "Output",
  browser: "Live Chrome",
};

/** Resolve the initial view from the Telegram `start_param`; default to diff. */
export function resolveView(startParam: string | undefined): ViewId {
  if (startParam && (VIEWS as readonly string[]).includes(startParam)) {
    return startParam as ViewId;
  }
  return "diff";
}
