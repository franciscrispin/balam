/**
 * Minimal typings for @novnc/novnc (it ships none) — only the surface the
 * browser view uses. The package's `exports` maps the bare specifier to
 * `core/rfb.js`, whose default export is the RFB client class. Shape adapted
 * from the open-shrimp reference (ADR-0011).
 */
declare module "@novnc/novnc" {
  export default class RFB extends EventTarget {
    constructor(
      target: HTMLElement,
      urlOrChannel: string | WebSocket,
      options?: { shared?: boolean },
    );
    viewOnly: boolean;
    scaleViewport: boolean;
    clipViewport: boolean;
    background: string;
    disconnect(): void;
  }
}
