/**
 * Shares the launch-time workspace context (from the deep link) with the views,
 * which are code-split siblings of the app shell rather than direct children.
 */
import { createContext, useContext } from "react";

export interface LaunchContextValue {
  /** Workspace context name to scope data to; undefined → backend default. */
  context: string | undefined;
  /** Ephemeral markdown snapshot id for the markdown view. */
  content: string | undefined;
}

const LaunchContext = createContext<LaunchContextValue>({
  context: undefined,
  content: undefined,
});

export const LaunchProvider = LaunchContext.Provider;

export function useLaunchContext(): LaunchContextValue {
  return useContext(LaunchContext);
}
