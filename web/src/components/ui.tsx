import { type ReactNode, useCallback, useEffect, useState } from "react";

import { ApiError } from "../api/client";

export interface Loaded<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

/** Load data on mount (and on `reload`); a late response never overwrites a newer one. */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[]): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [version, setVersion] = useState(0);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(load, deps);
  useEffect(() => {
    let live = true;
    setLoading(true);
    run().then(
      (value) => {
        if (!live) return;
        setData(value);
        setError(null);
        setLoading(false);
      },
      (reason: unknown) => {
        if (!live) return;
        setError(reason);
        setLoading(false);
      },
    );
    return () => {
      live = false;
    };
  }, [run, version]);
  return { data, error, loading, reload: () => setVersion((v) => v + 1) };
}

export function message(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return "Network error: the gateway could not be reached.";
  return "Something went wrong.";
}

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <p role="alert" className="error">
      {message(error)}
    </p>
  );
}

export function Notice({ children }: { children: ReactNode }) {
  return (
    <p role="status" className="notice">
      {children}
    </p>
  );
}

export function Loading() {
  return <p aria-busy="true">Loading…</p>;
}

export function Usage({ used, limit, label }: { used: number; limit: number; label: string }) {
  const max = Math.max(limit, 1);
  return (
    <meter
      aria-label={label}
      min={0}
      max={max}
      value={Math.min(used, max)}
      low={max * 0.8}
      high={max * 0.95}
      optimum={0}
    />
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  );
}
