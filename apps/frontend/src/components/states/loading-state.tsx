import { Skeleton } from "@/components/ui/skeleton";

/** Quiet skeleton rows. No full-screen spinners (design-system §6.5). */
export function LoadingState() {
  return (
    <div className="space-y-3">
      <Skeleton className="h-5 w-1/3" />
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-5/6" />
      <Skeleton className="h-4 w-2/3" />
    </div>
  );
}
