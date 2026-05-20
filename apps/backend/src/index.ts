import type { AppInfo } from "@balam/shared";

const info: AppInfo = {
  name: "balam-backend",
  version: "0.0.0",
};

// TODO (see docs/architecture-decisions.md):
//   - health-check the OpenCode server and wait for it to be ready (ADR-0001)
//   - start the grammY bot with the single-user ID allowlist (ADR-0008)
//   - serve the Mini App and reverse-proxy the noVNC WebSocket (ADR-0006)
//   - map Telegram forum topics to OpenCode sessions (ADR-0009)
console.log(`[${info.name}] v${info.version} — scaffold ready.`);
