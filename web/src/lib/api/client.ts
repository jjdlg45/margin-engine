/** Adapter selection (Run 4, 26.1): two implementations of one DataClient
 * contract. `static` reads the exported JSON files (GitHub Pages); `http`
 * talks to the BFF (docs/api/openapi.yaml, Phase 30). Selected at build time
 * by VITE_DATA_SOURCE; anything but "http" means static, so the hosted demo
 * can never silently depend on a server. */

import { httpAdapter } from "./httpAdapter";
import { sandboxAdapter } from "./sandboxAdapter";
import type { DataClient } from "./types";

export const dataSource: "static" | "http" =
  import.meta.env.VITE_DATA_SOURCE === "http" ? "http" : "static";

/** The static demo runs as a SANDBOX: the full review workflow works with the
 * same guardrails as the API, but every change stays in the visitor's browser
 * (localStorage); business numbers still come only from the exported files. */
export const isSandbox = dataSource !== "http";

export const dataClient: DataClient = dataSource === "http" ? httpAdapter : sandboxAdapter;
