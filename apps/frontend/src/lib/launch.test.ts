/**
 * `resolveLaunch` decides what the Mini App shows and which workspace it acts
 * on, from two sources that can disagree — URL query params and Telegram's
 * single `start_param` token. The precedence between them, and the `c_` marker
 * that redirects the markdown view to an ephemeral snapshot, are the parts worth
 * pinning down: getting either wrong points the app at the wrong workspace.
 */
import { beforeEach, describe, expect, test } from "bun:test";

import { resolveLaunch } from "./launch";
import { resolveView } from "./views";

/** `resolveLaunch` reads `window.location.search`; there is no DOM under bun test. */
function setSearch(search: string): void {
  (globalThis as { window?: unknown }).window = { location: { search } };
}

beforeEach(() => setSearch(""));

describe("resolveView", () => {
  test("accepts each known view", () => {
    expect(resolveView("diff")).toBe("diff");
    expect(resolveView("markdown")).toBe("markdown");
    expect(resolveView("browser")).toBe("browser");
  });

  test("falls back to diff for anything unknown", () => {
    expect(resolveView(undefined)).toBe("diff");
    expect(resolveView("")).toBe("diff");
    expect(resolveView("nope")).toBe("diff");
  });
});

describe("resolveLaunch", () => {
  test("reads view and context from query params", () => {
    setSearch("?view=markdown&context=balam");
    expect(resolveLaunch(undefined)).toEqual({
      view: "markdown",
      context: "balam",
      content: undefined,
    });
  });

  test("decodes the view__context start_param when there are no query params", () => {
    expect(resolveLaunch("browser__balam")).toEqual({
      view: "browser",
      context: "balam",
      content: undefined,
    });
  });

  test("query params win over the start_param", () => {
    setSearch("?view=diff&context=fromquery");
    expect(resolveLaunch("browser__fromparam")).toEqual({
      view: "diff",
      context: "fromquery",
      content: undefined,
    });
  });

  test("a c_ token is a content id, not a context", () => {
    expect(resolveLaunch("markdown__c_deadbeef")).toEqual({
      view: "markdown",
      context: undefined,
      content: "deadbeef",
    });
  });

  test("a context that merely starts with c_ is still a context", () => {
    // The marker requires hex after c_, so a real context name is not swallowed.
    expect(resolveLaunch("diff__c_notthemarker")).toEqual({
      view: "diff",
      context: "c_notthemarker",
      content: undefined,
    });
  });

  test("a start_param with no second token leaves the context unset", () => {
    expect(resolveLaunch("markdown")).toEqual({
      view: "markdown",
      context: undefined,
      content: undefined,
    });
  });

  test("an unknown view in the start_param still falls back to diff", () => {
    expect(resolveLaunch("bogus__balam")).toEqual({
      view: "diff",
      context: "balam",
      content: undefined,
    });
  });

  test("no query params and no start_param is the plain default", () => {
    expect(resolveLaunch(undefined)).toEqual({
      view: "diff",
      context: undefined,
      content: undefined,
    });
  });
});
