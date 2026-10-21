/** TanStack Query layer: the only way components read data. Retries are
 * capped at 2 with exponential backoff and never fire on 4xx; stale data is
 * kept for the session (static files do not change underneath us). */

import { QueryClient, useQuery } from "@tanstack/react-query";
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

export function useGuidanceExplanation(customerId: string | null, itemId: string | null) {
  return useQuery({
    queryKey: ["guidance-explanation", customerId, itemId],
    queryFn: () => dataClient.getGuidanceExplanation(customerId!, itemId!),
    enabled: Boolean(customerId && itemId),
  });
}
