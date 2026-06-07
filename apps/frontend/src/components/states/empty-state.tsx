/** A calm serif sentence — no illustration (design-system §6.5). */
export function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex h-full min-h-40 items-center justify-center px-4 text-center">
      <p className="font-serif text-[1.95rem] leading-[2.4rem] text-muted-foreground">{message}</p>
    </div>
  );
}
