/** TanStack Query layer: the only way components read data. Retries are
 * capped at 2 with exponential backoff and never fire on 4xx; stale data is
 * kept for the session (static files do not change underneath us). */

import { QueryClient, useQuery } from "@tanstack/react-query";
import { useEffect, useReducer } from "react";
import { dataClient } from "./api/client";
import { ApiError, PageDataMap, PageName, TableName, TableQuery } from "./api/types";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: Infinity,
      retry: (failureCount, error) => {
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
        return failureCount < 2;
      },
      retryDelay: (attempt) => Math.min(500 * 2 ** attempt, 4000),
    },
  },
});

export function useAppData() {
  return useQuery({ queryKey: ["app"], queryFn: () => dataClient.getApp() });
}

export function usePageData<P extends PageName>(page: P) {
  return useQuery<PageDataMap[P]>({
    queryKey: ["page", page],
    queryFn: () => dataClient.getPage(page),
  });
}

export function useTableQuery<T extends TableName>(table: T, query: TableQuery) {
  return useQuery({
    queryKey: ["table", table, query],
    queryFn: () => dataClient.queryTable(table, query),
    placeholderData: (prev) => prev,
  });
}

// ---- Action center (Run 5, Phase 42) ---------------------------------------

export function useActionDetail(actionId: string | null) {
  return useQuery({
    queryKey: ["action-detail", actionId],
    queryFn: () => dataClient.actions.getDetail(actionId!),
    enabled: Boolean(actionId),
  });
}

export function useActionUsers() {
  return useQuery({
    queryKey: ["action-users"],
    queryFn: () => dataClient.actions.getUsers(),
    enabled: !dataClient.actions.readOnly,
  });
}

/** Robinson-Patman review flags with their supporting facts.
 *
 * Served on the actions page payload, because the reviewer who needs them is
 * looking at a flagged action. Absent sections are an empty list, never an
 * error: not every mapping produces flags. */
export function useComplianceReview() {
  const page = usePageData("actions");
  return {
    ...page,
    data: page.data?.compliance?.pairs ?? [],
  };
}

export function useActionOutcomes() {
  return useQuery({
    queryKey: ["action-outcomes"],
    queryFn: () => dataClient.actions.getOutcomes(),
  });
}

/** The working identity for review operations (a configured user name - local
 * identity, not authentication). Kept in a tiny module store so every
 * component sees the same choice; localStorage is a per-viewer convenience
 * only and every access is guarded. */
let currentUser: string | null = (() => {
  try {
    return localStorage.getItem("margin-engine-user");
  } catch {
    return null;
  }
})();
const userListeners = new Set<() => void>();

export function useCurrentUser(): [string | null, (u: string | null) => void] {
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    const cb = () => force();
    userListeners.add(cb);
    return () => {
      userListeners.delete(cb);
    };
  }, []);
  const set = (u: string | null) => {
    currentUser = u;
    try {
      if (u) localStorage.setItem("margin-engine-user", u);
      else localStorage.removeItem("margin-engine-user");
    } catch {
      /* storage unavailable: the choice still works for this session */
    }
    userListeners.forEach((cb) => cb());
  };
  return [currentUser, set];
}

export function useGuidanceExplanation(customerId: string | null, itemId: string | null) {
  return useQuery({
    queryKey: ["guidance-explanation", customerId, itemId],
    queryFn: () => dataClient.getGuidanceExplanation(customerId!, itemId!),
    enabled: Boolean(customerId && itemId),
  });
}
