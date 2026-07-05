import { useMemo } from "react";
import { AppShell } from "@/components/app-shell/app-shell";
import { resolveLaunch } from "@/lib/launch";
import { initTelegram } from "@/lib/telegram";

export function App() {
  // Init Telegram once and resolve the launch: initial view + workspace context
  // from the deep link (query params, with start_param as a fallback).
  const launch = useMemo(() => resolveLaunch(initTelegram()), []);
  return <AppShell initialView={launch.view} context={launch.context} content={launch.content} />;
}
