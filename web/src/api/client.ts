import type { Session } from "./types";

/** A failed API call, in the gateway's `{"error": {message, type, code, param}}` shape. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly param: string | null;

  constructor(status: number, code: string, message: string, param: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.param = param;
  }
}

// The access token lives in memory only. The refresh token survives a reload of this tab in
// sessionStorage (never localStorage), and the page runs under a strict Content-Security-Policy.
const REFRESH_KEY = "finops.refresh";
const EARLY_REFRESH_MS = 30_000;

interface State {
  session: Session;
  accessExpiresAt: number;
}

let state: State | null = null;
let inflight: Promise<Session | null> | null = null;
const listeners = new Set<(session: Session | null) => void>();

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null; // storage disabled: the session then lasts until the tab reloads
  }
}

export function currentSession(): Session | null {
  return state?.session ?? null;
}

export function onSessionChange(listener: (session: Session | null) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function publish(): void {
  for (const listener of listeners) listener(currentSession());
}

export function startSession(session: Session): void {
  state = { session, accessExpiresAt: Date.now() + session.expires_in * 1000 };
  storage()?.setItem(REFRESH_KEY, session.refresh_token);
  publish();
}

export function endSession(): void {
  state = null;
  storage()?.removeItem(REFRESH_KEY);
  publish();
}

export function hasStoredSession(): boolean {
  return Boolean(storage()?.getItem(REFRESH_KEY));
}

async function parse<T>(response: Response): Promise<T> {
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  let body: unknown;
  try {
    body = text ? JSON.parse(text) : undefined;
  } catch {
    throw new ApiError(response.status, `http_${response.status}`, "unexpected response from the server");
  }
  if (!response.ok) {
    const error = (body as { error?: { code?: string; message?: string; param?: string | null } })?.error;
    throw new ApiError(
      response.status,
      error?.code ?? `http_${response.status}`,
      error?.message ?? (response.statusText || "request failed"),
      error?.param ?? null,
    );
  }
  return body as T;
}

export async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return parse<T>(response);
}

async function exchange(): Promise<Session | null> {
  const token = storage()?.getItem(REFRESH_KEY);
  if (!token) return null;
  try {
    const session = await post<Session>("/v1/auth/refresh", { refresh_token: token });
    startSession(session);
    return session;
  } catch (error) {
    if (error instanceof ApiError && error.status < 500) {
      endSession(); // expired, revoked or reused: sign in again
      return null;
    }
    throw error;
  }
}

/** Trade the refresh token for a new session. Single-flight: refresh tokens are single-use and a
 * reuse revokes the whole token family, so concurrent callers must share one exchange. */
export function refresh(): Promise<Session | null> {
  inflight ??= exchange().finally(() => {
    inflight = null;
  });
  return inflight;
}

export async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  if (state !== null && Date.now() > state.accessExpiresAt - EARLY_REFRESH_MS) await refresh();
  const send = () =>
    fetch(path, {
      method,
      headers: {
        ...(body === undefined ? {} : { "content-type": "application/json" }),
        ...(state === null ? {} : { authorization: `Bearer ${state.session.access_token}` }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  let response = await send();
  if (response.status === 401 && state !== null && (await refresh()) !== null) {
    response = await send();
  }
  return parse<T>(response);
}

export async function signOut(): Promise<void> {
  const token = storage()?.getItem(REFRESH_KEY);
  endSession();
  if (token) await post("/v1/auth/logout", { refresh_token: token }).catch(() => undefined);
}
