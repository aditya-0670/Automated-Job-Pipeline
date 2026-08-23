/**
 * The browser's view of the gateway.
 *
 * Every call goes through `request`, so there is exactly one place that attaches
 * the bearer token and one place that turns the gateway's error envelope into a
 * thrown `ApiError`. A screen that forgets error handling then still shows a
 * real message rather than "undefined".
 */

import type { Profile, SessionStatus } from "./types";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:4000";
const TOKEN_KEY = "resumeforge.token";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details?: unknown,
  ) {
    super(message);
  }
}

export function storedToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

/**
 * In dev-auth mode the gateway mints a token for the seeded user. This is the
 * only place the frontend knows anything about how tokens are obtained, so
 * swapping in Google OAuth later is a change to this function and nothing else.
 */
export async function ensureToken(): Promise<string> {
  const existing = storedToken();
  if (existing) return existing;

  const response = await fetch(`${BASE}/api/auth/dev-token`, { method: "POST" });
  if (!response.ok) {
    throw new ApiError(response.status, "no_dev_token", "Could not obtain a dev token. Is the API running, and has the database been seeded?");
  }
  const { token } = (await response.json()) as { token: string };
  window.localStorage.setItem(TOKEN_KEY, token);
  return token;
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await ensureToken();
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${token}`,
      ...(init.headers as Record<string, string> | undefined),
    },
  });

  if (response.status === 401) {
    // The stored token is stale -- most often after the database was reseeded
    // and the user id changed. Dropping it means the next call re-mints one
    // instead of failing forever.
    clearToken();
  }
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as
      | { error?: { code?: string; message?: string; details?: unknown } }
      | null;
    throw new ApiError(
      response.status,
      body?.error?.code ?? "unknown",
      body?.error?.message ?? `Request failed (${response.status})`,
      body?.error?.details,
    );
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// ── Profile ──
export const getProfile = () =>
  request<{ profile: Profile; hasLatexTemplate: boolean }>("/api/profile");

export const getLatexTemplate = async (): Promise<string> => {
  const token = await ensureToken();
  const response = await fetch(`${BASE}/api/profile/latex`, {
    headers: { authorization: `Bearer ${token}` },
  });
  if (response.status === 404) return "";
  if (!response.ok) throw new ApiError(response.status, "latex", "Could not read your template");
  return response.text();
};

export const putLatexTemplate = (latexTemplate: string) =>
  request<{ ok: true; bytes: number }>("/api/profile/latex", {
    method: "PUT",
    body: JSON.stringify({ latexTemplate }),
  });

export const putSkills = (skills: { name: string; category: string }[]) =>
  request<{ ok: true; count: number }>("/api/profile/skills", {
    method: "PUT",
    body: JSON.stringify({ skills }),
  });

export const getGitHubStatus = () =>
  request<{ connected: boolean; username: string | null; lastSyncedAt: string | null; fresh: boolean; syncedProjects: number }>(
    "/api/profile/github",
  );

export const syncGitHub = (force = false) =>
  request<{ status: string; created: number; updated: number; apiRequests: number }>(
    `/api/profile/github/sync?force=${force}`,
    { method: "POST" },
  );

// ── Sessions ──
export const createSession = (body: { jobUrl?: string; jobText?: string }) =>
  request<{ sessionId: string; streamUrl: string }>("/api/sessions", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const listSessions = () =>
  request<{ sessions: { id: string; jobUrl: string | null; status: string; currentStep: string; createdAt: string }[] }>(
    "/api/sessions",
  );

export const getSession = (id: string, includeDiff = true) =>
  request<SessionStatus>(`/api/sessions/${id}?includeDiff=${includeDiff}`);

export const confirmKeywords = (id: string, keywords?: { term: string }[]) =>
  request<unknown>(`/api/sessions/${id}/keywords`, {
    method: "POST",
    body: JSON.stringify(keywords ? { keywords } : {}),
  });

export const submitReview = (
  id: string,
  body: { decision: string; changeRequest?: string; editedLatex?: string },
) =>
  request<unknown>(`/api/sessions/${id}/review`, {
    method: "POST",
    body: JSON.stringify(body),
  });

/**
 * The PDF and the event stream both need the token in the URL, because neither
 * an `<iframe>` nor `EventSource` can set an Authorization header. The gateway
 * accepts a query token on exactly these two routes and nowhere else.
 */
export function pdfUrl(id: string, token: string): string {
  return `${BASE}/api/sessions/${id}/pdf?token=${encodeURIComponent(token)}`;
}

/**
 * Callers must have awaited `ensureToken()` first -- `useSession` does. Reading
 * the token synchronously on a cold load yields an empty string and a silent 401
 * the stream never retries.
 *
 * `afterSequence` is the last event this client has already seen. `EventSource`
 * sends `Last-Event-ID` on its *own* reconnects but not on a connection we open
 * ourselves, so when the page reopens the stream after answering a gate it must
 * say where it got to. Without it the server sees a caught-up client as a brand
 * new reader, reports the stop it is still sitting in, and the page waits
 * forever for a pipeline that already moved on.
 */
export function streamUrl(id: string, afterSequence = 0): string {
  const token = encodeURIComponent(storedToken() ?? "");
  return `${BASE}/api/sessions/${id}/stream?token=${token}&lastEventId=${afterSequence}`;
}
