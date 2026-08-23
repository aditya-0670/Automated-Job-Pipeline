/**
 * The only thing that talks to the AI service.
 *
 * Two responsibilities, and the first is the interesting one:
 *
 *   1. **Enrichment.** The AI service fetches no user data — it holds no
 *      database credentials and cannot reach Postgres for user rows. Every run
 *      is handed the profile and the LaTeX template, assembled here from the
 *      gateway's own database (`toAiProfile`). That is why one user's data can
 *      never leak into another's session: the pipeline only ever sees what this
 *      function put in the request.
 *   2. **Relay.** Progress and PDFs are streamed through rather than buffered,
 *      because a 60-second run buffered here is a 60-second wait for the user
 *      and a resume-sized string held in memory per session.
 */

import { getConfig } from "../config.js";
import { logger } from "../logger.js";
import { ApiError } from "../middleware/errors.js";

import type { AiProfile } from "../profile.js";

export interface RunPipelineInput {
  sessionId: string;
  userId: string;
  userLatex: string;
  userProfile: AiProfile;
  jobUrl?: string;
  jobText?: string;
  maxIterations?: number;
}

export interface ReviewInput {
  decision: "accept" | "request_changes" | "edit" | "modify_keywords";
  changeRequest?: string;
  editedLatex?: string;
}

export interface AiClient {
  run(input: RunPipelineInput): Promise<{ session_id: string; status: string }>;
  resumeKeywords(sessionId: string, keywords?: unknown[]): Promise<unknown>;
  resumeReview(sessionId: string, input: ReviewInput): Promise<unknown>;
  status(sessionId: string, options?: { includeDiff?: boolean }): Promise<Record<string, unknown>>;
  events(sessionId: string, options: { lastEventId?: string; signal: AbortSignal }): Promise<Response>;
  pdf(sessionId: string): Promise<Response>;
}

/** Requests carry the correlation id so one user action is one grep across both
 *  services; the AI service adopts an inbound `x-request-id` rather than minting
 *  its own. */
function headers(requestId?: string): Record<string, string> {
  const h: Record<string, string> = {
    "content-type": "application/json",
    "x-internal-key": getConfig().INTERNAL_API_KEY,
  };
  if (requestId) h["x-request-id"] = requestId;
  return h;
}

/** A pipeline node can take tens of seconds; only the *call* is bounded here,
 *  and the calls this client makes all return immediately (202) or read state. */
const REQUEST_TIMEOUT_MS = 15_000;

async function call(
  path: string,
  init: RequestInit & { requestId?: string } = {},
): Promise<Response> {
  const { AI_SERVICE_URL } = getConfig();
  const { requestId, ...rest } = init;
  const url = `${AI_SERVICE_URL}${path}`;

  let response: Response;
  try {
    response = await fetch(url, {
      ...rest,
      headers: { ...headers(requestId), ...(rest.headers as Record<string, string>) },
      signal: rest.signal ?? AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (err) {
    // A connection failure to an internal dependency is a 502, not a 500: the
    // gateway is fine, the thing behind it is not, and the distinction is what
    // tells an operator which container to look at.
    logger.error({ err, url }, "AI service unreachable");
    throw ApiError.upstream("The AI service is not reachable right now.", 502);
  }
  return response;
}

async function json<T>(response: Response, context: string): Promise<T> {
  if (response.ok) return (await response.json()) as T;

  const body = await response.text();
  let detail = body;
  try {
    detail = JSON.parse(body).detail ?? body;
  } catch {
    // Not JSON: keep the raw text.
  }
  // Client errors from the AI service are the *user's* errors -- an unusable job
  // posting, a decision that does not match the session's state -- so they are
  // forwarded with their status instead of being flattened into a 502. Anything
  // 5xx is ours to own.
  const status = response.status >= 400 && response.status < 500 ? response.status : 502;
  throw new ApiError(status, status === 502 ? "upstream_error" : "pipeline_error", String(detail), {
    context,
  });
}

export function createAiClient(requestId?: string): AiClient {
  return {
    async run(input) {
      const response = await call("/internal/pipeline/run", {
        method: "POST",
        requestId,
        body: JSON.stringify({
          session_id: input.sessionId,
          user_id: input.userId,
          user_latex: input.userLatex,
          user_profile: input.userProfile,
          job_url: input.jobUrl ?? "",
          job_text: input.jobText ?? "",
          max_iterations: input.maxIterations ?? 3,
        }),
      });
      return json(response, "run");
    },

    async resumeKeywords(sessionId, keywords) {
      const response = await call(`/internal/pipeline/${sessionId}/resume`, {
        method: "POST",
        requestId,
        body: JSON.stringify(keywords === undefined ? {} : { keywords }),
      });
      return json(response, "resume:keywords");
    },

    async resumeReview(sessionId, input) {
      const response = await call(`/internal/pipeline/${sessionId}/resume`, {
        method: "POST",
        requestId,
        body: JSON.stringify({
          decision: input.decision,
          change_request: input.changeRequest ?? "",
          edited_latex: input.editedLatex ?? "",
        }),
      });
      return json(response, "resume:review");
    },

    async status(sessionId, options = {}) {
      const query = options.includeDiff === false ? "?include_diff=false" : "";
      const response = await call(`/internal/pipeline/${sessionId}${query}`, { requestId });
      return json(response, "status");
    },

    async events(sessionId, { lastEventId, signal }) {
      // No timeout: this connection is meant to stay open. The abort signal is
      // wired to the browser's disconnect instead, which is the only thing that
      // should end it.
      return call(`/internal/pipeline/${sessionId}/events`, {
        requestId,
        signal,
        headers: lastEventId ? { "last-event-id": lastEventId } : {},
      });
    },

    async pdf(sessionId) {
      return call(`/internal/pipeline/${sessionId}/pdf`, { requestId });
    },
  };
}
