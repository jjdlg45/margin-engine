/** Bundle loading: fetched once, provided via context. The app renders only
 * what the engine exported. */

import { createContext, useContext, useEffect, useState } from "react";
import type { WebBundle } from "../types/bundle";

type State =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; bundle: WebBundle };

const BundleContext = createContext<State>({ status: "loading" });

export function BundleProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<State>({ status: "loading" });
  useEffect(() => {
    fetch(`${import.meta.env.BASE_URL}data/bundle.json`)
      .then(async (r) => {
        if (!r.ok) throw new Error(`bundle fetch failed: ${r.status}`);
        setState({ status: "ready", bundle: (await r.json()) as WebBundle });
      })
      .catch((e: Error) => setState({ status: "error", message: e.message }));
  }, []);
  return <BundleContext.Provider value={state}>{children}</BundleContext.Provider>;
}

export function useBundleState(): State {
  return useContext(BundleContext);
}

export function useBundle(): WebBundle {
  const s = useContext(BundleContext);
  if (s.status !== "ready") throw new Error("bundle not ready");
  return s.bundle;
}
