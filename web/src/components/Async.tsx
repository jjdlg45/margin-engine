/** Loading, error and boundary primitives. Partial data never renders as
 * complete: a section shows its skeleton until its query settles, and a
 * plain-English error state with Retry when it fails. */

import { Component, ReactNode } from "react";
import type { UseQueryResult } from "@tanstack/react-query";

export function Skeleton({ lines = 3, label }: { lines?: number; label?: string }) {
  return (
    <div role="status" aria-label={label ?? "Loading"} className="animate-pulse py-2">
      <span className="sr-only">Loading{label ? ` ${label}` : ""}…</span>
      {Array.from({ length: lines }, (_, i) => (
        <div
          key={i}
          aria-hidden="true"
          className="mb-2 h-4 rounded-badge bg-ink/[0.08]"
          style={{ width: `${90 - (i % 3) * 18}%` }}
        />
      ))}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message?: string; onRetry: () => void }) {
  return (
    <div role="alert" className="rounded-[8px] border border-garnet/40 bg-garnet/5 px-5 py-4">
      <p className="m-0 font-medium">This section could not load.</p>
      <p className="mb-3 mt-1 max-w-xl text-caption secondary">
        {message ??
          "The data request failed. Your connection may have dropped, or the data files are unavailable."}
      </p>
      <button
        onClick={onRetry}
        className="rounded-badge border border-petrol bg-petrol px-3 py-1 text-sm text-paper"
      >
        Retry
      </button>
    </div>
  );
}

/** Render-prop wrapper: skeleton while loading, error state on failure, and
 * children only when the data is complete. `reserve` (Tailwind min-height
 * classes) holds the section's final height through the skeleton-to-content
 * swap so loading causes no layout shift. */
export function QuerySection<T>({
  query,
  label,
  lines,
  reserve,
  children,
}: {
  query: UseQueryResult<T>;
  label: string;
  lines?: number;
  reserve?: string;
  children: (data: T) => ReactNode;
}) {
  let body: ReactNode;
  if (query.isPending) body = <Skeleton label={label} lines={lines} />;
  else if (query.isError)
    body = <ErrorState message={query.error.message} onRetry={() => query.refetch()} />;
  else body = children(query.data);
  return reserve ? <div className={reserve}>{body}</div> : <>{body}</>;
}

interface BoundaryState {
  error: Error | null;
}

/** React error boundary used per route and per major section. */
export class Boundary extends Component<{ label: string; children: ReactNode }, BoundaryState> {
  state: BoundaryState = { error: null };
  static getDerivedStateFromError(error: Error): BoundaryState {
    return { error };
  }
  render() {
    if (this.state.error) {
      return (
        <ErrorState
          message={`Something went wrong rendering ${this.props.label}. ${this.state.error.message}`}
          onRetry={() => this.setState({ error: null })}
        />
      );
    }
    return this.props.children;
  }
}
