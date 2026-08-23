/**
 * Gateway metrics.
 *
 * Deliberately thin. The pipeline's interesting numbers -- tokens, guardrail
 * failures, node timings -- belong to the AI service and are exported there;
 * duplicating them here would produce two sources of truth that drift. What the
 * gateway uniquely knows is the shape of traffic reaching the system and how
 * often it refuses it.
 */

import { collectDefaultMetrics, Counter, Histogram, Registry } from "prom-client";

import type { RequestHandler } from "express";

export const registry = new Registry();

// Event-loop lag, heap, GC. The default set is worth having because a gateway
// that relays SSE holds many idle connections, and lag is the first symptom.
collectDefaultMetrics({ register: registry, prefix: "resumeforge_api_" });

const httpDuration = new Histogram({
  name: "resumeforge_api_request_duration_seconds",
  help: "HTTP request duration.",
  // `route`, not `path`: a path label would put every session id in its own
  // series, which is unbounded cardinality and the classic way to kill a
  // Prometheus instance.
  labelNames: ["method", "route", "status"],
  buckets: [0.005, 0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10],
  registers: [registry],
});

const authFailures = new Counter({
  name: "resumeforge_api_auth_failures_total",
  help: "Requests rejected by authentication, by reason.",
  labelNames: ["reason"],
  registers: [registry],
});

const rateLimited = new Counter({
  name: "resumeforge_api_rate_limited_total",
  help: "Requests refused by a rate limiter, by limiter name.",
  labelNames: ["limiter"],
  registers: [registry],
});

const streams = new Counter({
  name: "resumeforge_api_event_streams_total",
  help: "SSE streams opened and closed. A growing gap means leaked connections.",
  labelNames: ["event"], // opened | closed
  registers: [registry],
});

export function recordAuthFailure(reason: string): void {
  authFailures.labels(reason).inc();
}

export function recordRateLimited(limiter: string): void {
  rateLimited.labels(limiter).inc();
}

export function recordStream(event: "opened" | "closed"): void {
  streams.labels(event).inc();
}

/**
 * Time every request against its *route pattern*.
 *
 * Express only knows the matched route after the handler runs, which is why
 * this reads `req.route` on finish rather than `req.path` up front.
 */
export const metricsMiddleware: RequestHandler = (req, res, next) => {
  if (req.path === "/metrics") return next();
  const end = httpDuration.startTimer();
  res.on("finish", () => {
    const matched = (req.route as { path?: string } | undefined)?.path;
    const route = `${req.baseUrl ?? ""}${matched ?? ""}` || "unmatched";
    end({ method: req.method, route, status: String(res.statusCode) });
  });
  next();
};
