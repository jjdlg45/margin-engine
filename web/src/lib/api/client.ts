/** Adapter selection (Run 4, 26.1): two implementations of one DataClient
 * contract. `static` reads the exported JSON files (GitHub Pages); `http`
 * talks to the BFF (docs/api/openapi.yaml, Phase 30). Selected at build time
 * by VITE_DATA_SOURCE; anything but "http" means static, so the hosted demo
 * can never silently depend on a server. */

import { httpAdapter } from "./httpAdapter";
import { staticAdapter } from "./staticAdapter";
import type { DataClient } from "./types";

export const dataSource: "static" | "http" =
  import.meta.env.VITE_DATA_SOURCE === "http" ? "http" : "static";

export const dataClient: DataClient = dataSource === "http" ? httpAdapter : staticAdapter;
