import RFB from "@novnc/novnc";
import { RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ErrorState } from "@/components/states/error-state";
import { Button } from "@/components/ui/button";
import { ApiError, getBrowserStatus, getInitData } from "@/lib/api";

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
 * silent until the auth frame passes. View-only for now.
 */
export default function BrowserView() {
  const containerRef = useRef<HTMLDivElement>(null);
  const [conn, setConn] = useState<Conn>("checking");
  // Bumping this re-runs the effect: full teardown, fresh status probe + RFB.
  const [attempt, setAttempt] = useState(0);

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
      rfb.viewOnly = true;
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
      try {
        rfb?.disconnect();
      } catch {
        // already torn down
      }
    };
  }, [attempt]);

  const reconnect = () => setAttempt((n) => n + 1);
  const live = conn === "connected";

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
            <ErrorState message="Couldn't verify this Mini App session." />
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

      <div className="absolute inset-x-0 bottom-4 mx-auto flex w-fit items-center gap-3 rounded-lg border bg-card px-4 py-2 shadow-lg">
        <span className="flex items-center gap-2 text-sm text-muted-foreground">
          <span
            className={`size-2 rounded-full ${live ? "balam-pulse bg-clay" : "bg-muted-foreground/40"}`}
          />
          {live ? "Live" : conn === "checking" || conn === "connecting" ? "Connecting" : "Offline"}
        </span>
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
