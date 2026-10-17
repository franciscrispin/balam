/**
 * The hidden-input bridge turns edits of a text field into X11 key events for
 * the agent's browser. The diff and the keysym mapping are where a mistake
 * would silently type the wrong thing into a page, so they are pinned here.
 */
import { describe, expect, test } from "bun:test";

import { diffInput, ENTER_KEYSYM, INPUT_FILLER, keysymForChar } from "./vnc-keys";

describe("diffInput", () => {
  test("appended text is typed", () => {
    expect(diffInput(INPUT_FILLER, `${INPUT_FILLER}hi`)).toEqual({ erased: 0, typed: "hi" });
  });

  test("a backspace into the filler erases one", () => {
    expect(diffInput(INPUT_FILLER, INPUT_FILLER.slice(0, -1))).toEqual({ erased: 1, typed: "" });
  });

  test("an autocorrect replacement erases then retypes", () => {
    expect(diffInput("____teh", "____the ")).toEqual({ erased: 2, typed: "he " });
  });

  test("no change is a no-op", () => {
    expect(diffInput(INPUT_FILLER, INPUT_FILLER)).toEqual({ erased: 0, typed: "" });
  });
});

describe("keysymForChar", () => {
  test("ASCII and Latin-1 map to their code point", () => {
    expect(keysymForChar("a")).toBe(0x61);
    expect(keysymForChar("@")).toBe(0x40);
    expect(keysymForChar("é")).toBe(0xe9);
  });

  test("other Unicode uses the 0x01000000 offset", () => {
    expect(keysymForChar("€")).toBe(0x010020ac);
    expect(keysymForChar("😀")).toBe(0x0101f600);
  });

  test("newline is Enter", () => {
    expect(keysymForChar("\n")).toBe(ENTER_KEYSYM);
  });
});
