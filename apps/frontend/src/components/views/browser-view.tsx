import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";

/**
 * Placeholder for the live noVNC Chrome view (ADR-0006). The real iframe +
 * WebSocket reverse-proxy are deferred; this renders the connecting state
 * (serif label + pulsing clay dot) and the floating toolbar from §6.4.
 */
export default function BrowserView() {
  return (
    <div className="relative h-full min-h-80">
      <div className="flex h-full min-h-80 flex-col items-center justify-center gap-4 rounded-lg border border-dashed bg-card">
        <span className="balam-pulse size-2.5 rounded-full bg-clay" />
        <p className="font-serif text-[1.95rem] leading-none text-muted-foreground">Connecting…</p>
      </div>

      <div className="absolute inset-x-0 bottom-4 mx-auto flex w-fit items-center gap-3 rounded-lg border bg-card px-4 py-2 shadow-lg">
        <span className="flex items-center gap-2 text-sm text-muted-foreground">
          <span className="balam-pulse size-2 rounded-full bg-clay" />
          Live
        </span>
        <Button variant="ghost" size="icon" aria-label="Refresh" className="size-9">
          <RefreshCw className="size-4" />
        </Button>
      </div>
    </div>
  );
}
