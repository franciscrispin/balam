import type { ReactNode } from "react";

/**
 * Slim sticky top bar (48px). View title (label style) left, a menu affordance
 * right (design-system §6.1). Honors the top safe-area inset.
 */
export function TopBar({ title, actions }: { title: string; actions?: ReactNode }) {
  return (
    <header
      className="sticky top-0 z-20 flex h-12 items-center justify-between border-b bg-background/95 px-4 backdrop-blur"
      style={{ paddingTop: "env(safe-area-inset-top)" }}
    >
      <span className="font-sans text-xs font-semibold tracking-[0.02em] uppercase">{title}</span>
      {actions}
    </header>
  );
}
