import { expect, test } from "bun:test";
import type { AppInfo } from "@balam/shared";

// Smoke test: proves the shared workspace package resolves and its types are
// usable from the backend. Replace with real tests as features land.
test("shared AppInfo is usable across workspaces", () => {
  const info: AppInfo = { name: "balam-backend", version: "0.0.0" };
  expect(info.name).toBe("balam-backend");
});
