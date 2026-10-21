/** The one-file swap: the app talks to `dataClient`; going live replaces the
 * static adapter with an HTTP adapter against the BFF (docs/api/openapi.yaml)
 * on this line. */

import { staticAdapter } from "./staticAdapter";
import type { DataClient } from "./types";

export const dataClient: DataClient = staticAdapter;
