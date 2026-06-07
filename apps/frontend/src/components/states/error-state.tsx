import { Button } from "@/components/ui/button";

/**
 * Muted error text + optional Retry. Omit `onRetry` for unrecoverable cases
 * such as auth failure (ADR-0008), which should not offer a retry.
 */
export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex h-full min-h-40 flex-col items-center justify-center gap-4 px-4 text-center">
      <p className="text-sm text-muted-foreground">{message}</p>
      {onRetry ? (
        <Button variant="outline" onClick={onRetry}>
          Retry
        </Button>
      ) : null}
    </div>
  );
}
