import RFB from "@novnc/novnc";
import { Keyboard, MousePointerClick, RefreshCw } from "lucide-react";
import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";
import { ErrorState } from "@/components/states/error-state";
import { Button } from "@/components/ui/button";
import { ApiError, AUTH_ERROR_MESSAGE, getBrowserStatus, getInitData } from "@/lib/api";
import {
  BACKSPACE_KEYSYM,
  diffInput,
  INPUT_FILLER,
  keysymForChar,
  SPECIAL_KEYSYMS,
} from "@/lib/vnc-keys";

type Conn =
  | "checking" // probing /api/browser/status
  | "offline" // stack not running (no x11vnc to reach)
  | "auth-error" // initData rejected — not recoverable by retry (§7)
  | "connecting" // RFB handshake in flight
  | "connected" // live frames
  | "disconnected"; // was connected, then the WS/VNC side ended

/**
 * The live noVNC Chrome view (ADR-0006, as amended): the RFB client renders the
 * agent's X display straight into the container div — no iframe. Bytes ride
 * `/api/vnc/ws`, which the backend bridges to x11vnc after the client sends its
 * `initData` as the first text frame (a browser can't set an Authorization
 * header on a WebSocket, and a query param would leak into the server's access
 * log). Safe ordering: in RFB the server speaks first, and the backend stays
 * silent until the auth frame passes. View-only by default; the Control toggle
 * hands mouse/touch and keyboard to the viewer, and the keyboard button types
 * through a hidden input because a phone never opens its keyboard for a canvas.
 */
export default function BrowserView() {
  const containerRef = useRef<HTMLDivElement>(null);
  const [conn, setConn] = useState<Conn>("checking");
  // Bumping this re-runs the effect: full teardown, fresh status probe + RFB.
  const [attempt, setAttempt] = useState(0);
  const rfbRef = useRef<RFB | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [control, setControl] = useState(false);
  // Read when the RFB is (re)created, so a reconnect keeps the current mode.
  const controlRef = useRef(control);
  const [keyboardOpen, setKeyboardOpen] = useState(false);

  useEffect(() => {
    controlRef.current = control;
    if (rfbRef.current) rfbRef.current.viewOnly = !control;
    if (!control) inputRef.current?.blur();
  }, [control]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: `attempt` is a reconnect trigger — bumping it tears the RFB down (effect cleanup) and reconnects
  useEffect(() => {
    let cancelled = false;
    let rfb: RFB | null = null;

    (async () => {
      setConn("checking");
      try {
        const status = await getBrowserStatus();
        if (cancelled) return;
        if (!status.running) {
          setConn("offline");
          return;
        }
      } catch (err) {
        if (cancelled) return;
        setConn(err instanceof ApiError && err.isAuth ? "auth-error" : "offline");
        return;
      }

      const target = containerRef.current;
      if (!target) return;
      setConn("connecting");
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${window.location.host}/api/vnc/ws`);
      // Registered before RFB attaches its own handlers, so the auth frame is
      // the first thing on the wire.
      ws.addEventListener("open", () => ws.send(getInitData()), { once: true });
      rfb = new RFB(target, ws);
      rfbRef.current = rfb;
      rfb.viewOnly = !controlRef.current;
      rfb.scaleViewport = true; // fit the 1440×900 display into the webview
      rfb.clipViewport = false;
      rfb.background = "transparent";
      rfb.addEventListener("connect", () => {
        if (!cancelled) setConn("connected");
      });
      rfb.addEventListener("disconnect", () => {
        if (!cancelled) setConn("disconnected");
      });
    })();

    return () => {
      cancelled = true;
      rfbRef.current = null;
      try {
        rfb?.disconnect();
      } catch {
        // already torn down
      }
    };
  }, [attempt]);

  const reconnect = () => setAttempt((n) => n + 1);
  const live = conn === "connected";

  const sendKey = (keysym: number) => rfbRef.current?.sendKey(keysym, null);

  // Named keys (Enter, arrows, Tab, a hardware Backspace) arrive as keydown;
  // swallowing them keeps the filler intact. Text arrives through onInput.
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    const keysym = SPECIAL_KEYSYMS[e.key];
    if (keysym === undefined) return;
    e.preventDefault();
    sendKey(keysym);
  };

  const onInput = (e: FormEvent<HTMLInputElement>) => {
    // Wait for an IME/predictive-text composition to commit before diffing.
    if (!(e.nativeEvent as InputEvent).isComposing) flushInput(e.currentTarget);
  };

  const flushInput = (field: HTMLInputElement) => {
    const { erased, typed } = diffInput(INPUT_FILLER, field.value);
    for (let i = 0; i < erased; i++) sendKey(BACKSPACE_KEYSYM);
    for (const char of typed) sendKey(keysymForChar(char));
    field.value = INPUT_FILLER;
  };

  return (
    <div className="relative h-full min-h-80">
      {/* RFB renders into this div; it must stay mounted in every state, so the
          non-live states overlay it instead of replacing it. */}
      <div
        ref={containerRef}
        className={`h-full min-h-80 overflow-hidden rounded-lg border bg-card ${live ? "" : "invisible"}`}
      />

      {!live && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 rounded-lg border border-dashed bg-card">
          {conn === "checking" || conn === "connecting" ? (
            <>
              <span className="balam-pulse size-2.5 rounded-full bg-clay" />
              <p className="font-serif text-[1.95rem] leading-none text-muted-foreground">
                Connecting…
              </p>
            </>
          ) : conn === "auth-error" ? (
            <ErrorState message={AUTH_ERROR_MESSAGE} />
          ) : (
            <ErrorState
              message={
                conn === "offline"
                  ? "No live browser session. The agent isn't running a browser right now."
                  : "Live view disconnected."
              }
              onRetry={reconnect}
            />
          )}
        </div>
      )}

      {/* Off-screen but focusable: 16px stops iOS zooming in on focus. */}
      <input
        ref={inputRef}
        type="text"
        defaultValue={INPUT_FILLER}
        aria-hidden
        tabIndex={-1}
        autoCapitalize="off"
        autoComplete="off"
        autoCorrect="off"
        spellCheck={false}
        className="pointer-events-none absolute bottom-0 left-0 size-px text-[16px] opacity-0"
        onKeyDown={onKeyDown}
        onInput={onInput}
        onCompositionEnd={(e) => flushInput(e.currentTarget)}
        onFocus={() => setKeyboardOpen(true)}
        onBlur={() => setKeyboardOpen(false)}
      />

      <div className="absolute inset-x-0 bottom-4 mx-auto flex w-fit items-center gap-3 rounded-lg border bg-card px-4 py-2 shadow-lg">
        <span className="flex items-center gap-2 text-sm text-muted-foreground">
          <span
            className={`size-2 rounded-full ${live ? "balam-pulse bg-clay" : "bg-muted-foreground/40"}`}
          />
          {live ? "Live" : conn === "checking" || conn === "connecting" ? "Connecting" : "Offline"}
        </span>
        {live && (
          <Button
            variant={control ? "default" : "ghost"}
            size="icon"
            aria-label={control ? "Stop controlling" : "Take control"}
            aria-pressed={control}
            className="size-9"
            onClick={() => setControl((c) => !c)}
          >
            <MousePointerClick className="size-4" />
          </Button>
        )}
        {live && control && (
          <Button
            variant={keyboardOpen ? "default" : "ghost"}
            size="icon"
            aria-label={keyboardOpen ? "Hide keyboard" : "Show keyboard"}
            aria-pressed={keyboardOpen}
            className="size-9"
            // Keep focus where it is, so a tap that closes the keyboard isn't
            // undone by the button stealing focus first.
            onPointerDown={(e) => e.preventDefault()}
            onClick={() => (keyboardOpen ? inputRef.current?.blur() : inputRef.current?.focus())}
          >
            <Keyboard className="size-4" />
          </Button>
        )}
        <Button
          variant="ghost"
          size="icon"
          aria-label="Refresh"
          className="size-9"
          onClick={reconnect}
        >
          <RefreshCw className="size-4" />
        </Button>
      </div>
    </div>
  );
}
