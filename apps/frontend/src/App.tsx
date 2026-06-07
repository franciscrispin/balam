import { useMemo } from "react";
import { AppShell } from "@/components/app-shell/app-shell";
import { initTelegram } from "@/lib/telegram";
import { resolveView } from "@/lib/views";

export function App() {
  // Init Telegram once and pick the initial view from the deep-link start_param.
  const initialView = useMemo(() => resolveView(initTelegram().startParam), []);
  return <AppShell initialView={initialView} />;
}
