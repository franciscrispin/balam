/**
 * Keyboard input for the live browser view's control mode. A phone only opens
 * its on-screen keyboard for a focused text field, never for the RFB canvas, so
 * the view types through a hidden input and forwards what lands in it as X11
 * keysyms via `rfb.sendKey`. `@novnc/novnc` exports only the RFB class, so the
 * keysym mapping lives here.
 */

/** Keys a `keydown` reports by name that have no text of their own. */
export const SPECIAL_KEYSYMS: Readonly<Record<string, number>> = {
  Backspace: 0xff08,
  Tab: 0xff09,
  Enter: 0xff0d,
  Escape: 0xff1b,
  Delete: 0xffff,
  Home: 0xff50,
  ArrowLeft: 0xff51,
  ArrowUp: 0xff52,
  ArrowRight: 0xff53,
  ArrowDown: 0xff54,
  PageUp: 0xff55,
  PageDown: 0xff56,
  End: 0xff57,
};

export const BACKSPACE_KEYSYM = SPECIAL_KEYSYMS.Backspace as number;
export const ENTER_KEYSYM = SPECIAL_KEYSYMS.Enter as number;

/**
 * The keysym for one character: Latin-1 code points are their own keysym, and
 * every other Unicode character maps to `0x01000000 | codepoint` (X11's rule).
 */
export function keysymForChar(char: string): number {
  if (char === "\n") return ENTER_KEYSYM;
  const cp = char.codePointAt(0) ?? 0;
  if ((cp >= 0x20 && cp <= 0x7e) || (cp >= 0xa0 && cp <= 0xff)) return cp;
  return 0x01000000 | cp;
}

/**
 * The hidden input is kept holding this filler between edits, so a backspace
 * on a mobile keyboard always has something to delete — Android fires no
 * usable `keydown` for it (key `Unidentified`), only the resulting `input`.
 */
export const INPUT_FILLER = "____";

/**
 * What an edit of the hidden input typed: how many characters it erased from
 * the end of `before`, then the text it added. Diffing the whole value (rather
 * than reading `InputEvent.data`) also covers autocorrect and IME commits.
 */
export function diffInput(before: string, after: string): { erased: number; typed: string } {
  let common = 0;
  while (common < before.length && common < after.length && before[common] === after[common]) {
    common++;
  }
  return { erased: before.length - common, typed: after.slice(common) };
}
