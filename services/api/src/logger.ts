/**
 * Structured logging, correlated with the AI service.
 *
 * The AI service already logs a `request_id` and echoes it on `x-request-id`
 * (services/ai/app/logging_config.py). The gateway mints that id and forwards
 * it, so one user action produces one grep across two languages -- which is the
 * only reason a correlation id is worth having.
 */

import { randomUUID } from "node:crypto";

// Named imports: under NodeNext resolution a default import of these CJS
// packages resolves to the module namespace, which is not callable.
import { pino } from "pino";
import { pinoHttp } from "pino-http";

import { getConfig } from "./config.js";

export const logger = pino({
  level: getConfig().LOG_LEVEL,
  base: { service: "resumeforge-api" },
  redact: {
    // A logged token is a leaked token, and these lines outlive the request.
    paths: ["req.headers.authorization", "req.headers['x-internal-key']", "*.githubToken"],
    censor: "[redacted]",
  },
});

export const httpLogger = pinoHttp({
  logger,
  genReqId: (req, res) => {
    const existing = req.headers["x-request-id"];
    const id = (Array.isArray(existing) ? existing[0] : existing) || randomUUID().slice(0, 16);
    res.setHeader("x-request-id", id);
    return id;
  },
  // Probes would otherwise be the majority of the log by volume and none of it
  // by value.
  autoLogging: { ignore: (req) => req.url === "/health" || req.url === "/ready" },
  customLogLevel: (_req, res, err) => {
    if (err || res.statusCode >= 500) return "error";
    if (res.statusCode >= 400) return "warn";
    return "info";
  },
});
