import type { DiffHunk } from "@balam/shared";
import { useCallback, useEffect, useState } from "react";
import { useLaunchContext } from "@/components/launch-context";
import { EmptyState } from "@/components/states/empty-state";
import { ErrorState } from "@/components/states/error-state";
import { LoadingState } from "@/components/states/loading-state";
import { ApiError, getDiff } from "@/lib/api";
import { HunkCard } from "./hunk-card";

type State =
  | { status: "loading" }
  | { status: "ready"; hunks: DiffHunk[] }
  | { status: "error"; message: string; recoverable: boolean };

export default function DiffView() {
  const { context } = useLaunchContext();
  const [state, setState] = useState<State>({ status: "loading" });

  // Returns a cancel cleanup so the effect drops a stale response on unmount /
  // context change; Retry re-invokes it directly.
  const load = useCallback(() => {
    let cancelled = false;
    setState({ status: "loading" });
    getDiff(context)
      .then((res) => {
        if (!cancelled) setState({ status: "ready", hunks: res.hunks });
      })
      .catch((err) => {
        if (cancelled) return;
        // Auth failures are unrecoverable here (no Retry, per design-system §7) —
        // re-fetching won't fix a bad/absent Mini App session.
        const isAuth = err instanceof ApiError && err.isAuth;
        setState({
          status: "error",
          message: isAuth ? "Couldn't verify this Mini App session." : "Couldn't load changes.",
          recoverable: !isAuth,
        });
      });
    return () => {
      cancelled = true;
    };
  }, [context]);

  useEffect(load, [load]);

  if (state.status === "loading") {
    return <LoadingState />;
  }
  if (state.status === "error") {
    return <ErrorState message={state.message} onRetry={state.recoverable ? load : undefined} />;
  }
  if (state.hunks.length === 0) {
    return (
      <section className="rounded-lg border border-dashed">
        <EmptyState message="No changes yet." />
      </section>
    );
  }
  return (
    <div className="space-y-6">
      {state.hunks.map((hunk) => (
        <HunkCard key={hunk.id} hunk={hunk} />
      ))}
    </div>
  );
}
