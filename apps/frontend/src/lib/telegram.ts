/**
 * Telegram Mini App init. Balam is fixed-light: we honor the webview lifecycle
 * (ready/expand) and pin the chrome to our paper background, but deliberately
 * ignore Telegram's themeParams so Balam looks the same in every chat theme
 * (docs/design-system.md §1, §8).
 */

const PAPER = "#faf9f5";

export interface TelegramInitResult {
  /** Deep-link payload (Mini App `start_param`), used to pick the initial view. */
  startParam: string | undefined;
  /** False when running outside the Telegram webview (e.g. plain browser dev). */
  available: boolean;
}

export function initTelegram(): TelegramInitResult {
  const tg = window.Telegram?.WebApp;
  if (!tg) {
    return { startParam: undefined, available: false };
  }

  tg.ready();
  tg.expand?.();
  tg.setBackgroundColor(PAPER);
  tg.setHeaderColor(PAPER);

  return { startParam: tg.initDataUnsafe?.start_param, available: true };
}
