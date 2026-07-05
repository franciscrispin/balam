/**
 * Telegram Mini App init. Balam is fixed-light: we honor the webview lifecycle
 * (ready/expand) and pin the chrome to our paper background, but deliberately
 * ignore Telegram's themeParams so Balam looks the same in every chat theme
 * (docs/design-system.md §1, §8).
 */

const PAPER = "#faf9f5";

/**
 * Init the webview and return the deep-link payload (Mini App `start_param`),
 * used to pick the initial view. Outside the Telegram webview (e.g. plain
 * browser dev) there is nothing to init and no payload.
 */
export function initTelegram(): string | undefined {
  const tg = window.Telegram?.WebApp;
  if (!tg) {
    return undefined;
  }

  tg.ready();
  tg.expand?.();
  tg.setBackgroundColor(PAPER);
  tg.setHeaderColor(PAPER);

  return tg.initDataUnsafe?.start_param;
}
