import type { AppInfo } from "@balam/shared";

const app: AppInfo = { name: "balam-mini-app", version: "0.0.0" };

export function App() {
  return (
    <main>
      <h1>Balam</h1>
      <p>
        {app.name} v{app.version} — Mini App scaffold. TODO: diff viewer, markdown viewer, and the
        live Chrome noVNC iframe (see docs/architecture-decisions.md).
      </p>
    </main>
  );
}
