import { MoreVertical } from "lucide-react";
import { type ComponentType, lazy, Suspense, useState } from "react";
import { LoadingState } from "@/components/states/loading-state";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { VIEW_TITLES, VIEWS, type ViewId } from "@/lib/views";
import { TopBar } from "./top-bar";

// Each view is its own code-split chunk; heavy deps (Shiki, react-markdown, and
// later the noVNC client) load only when that view is first opened.
const VIEW_COMPONENTS: Record<ViewId, ComponentType> = {
  diff: lazy(() => import("@/components/views/diff-view")),
  markdown: lazy(() => import("@/components/views/markdown-view")),
  browser: lazy(() => import("@/components/views/browser-view")),
};

export function AppShell({ initialView }: { initialView: ViewId }) {
  // The view is chosen on launch from the Telegram start_param (the bot deep-links
  // to the relevant surface). The menu offers switching as a secondary path —
  // and keeps every surface reachable in a plain browser, where there is no
  // start_param.
  const [view, setView] = useState<ViewId>(initialView);
  const Active = VIEW_COMPONENTS[view];

  return (
    <div className="flex h-full flex-col bg-background">
      <TopBar
        title={VIEW_TITLES[view]}
        actions={
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon" aria-label="Menu" className="-mr-2 size-11">
                <MoreVertical className="size-5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-44">
              <DropdownMenuLabel>View</DropdownMenuLabel>
              <DropdownMenuSeparator />
              <DropdownMenuRadioGroup
                value={view}
                onValueChange={(next) => setView(next as ViewId)}
              >
                {VIEWS.map((id) => (
                  <DropdownMenuRadioItem key={id} value={id}>
                    {VIEW_TITLES[id]}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        }
      />
      {/* key={view} re-triggers the mount rise+fade on each switch. */}
      <main key={view} className="balam-rise min-h-0 flex-1 overflow-auto px-4 pt-4 pb-4">
        <Suspense fallback={<LoadingState />}>
          <Active />
        </Suspense>
      </main>
    </div>
  );
}
