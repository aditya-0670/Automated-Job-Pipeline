/**
 * The Express app, built from injected dependencies.
 *
 * A factory rather than a module-level `app` so tests can supply a fake AI
 * client and a real database without starting a server or reaching the network.
 * `index.ts` is the only place that wires the real ones.
 */

import express from "express";

import { metricsMiddleware, registry } from "./metrics.js";
import { cors } from "./middleware/cors.js";
import { httpLogger } from "./logger.js";
import { errorHandler, notFoundHandler } from "./middleware/errors.js";
import { rateLimit } from "./middleware/rateLimit.js";
import { authRouter } from "./routes/auth.js";
import { profileRouter } from "./routes/profile.js";
import { sessionsRouter, type SessionDeps } from "./routes/sessions.js";
import { getConfig } from "./config.js";

export function createApp(deps: SessionDeps): express.Express {
  const app = express();
  const config = getConfig();

  // Behind nginx or an ELB, so `req.ip` must come from X-Forwarded-For or every
  // rate limit buckets the proxy instead of the user.
  app.set("trust proxy", true);
  app.disable("x-powered-by");

  app.use(httpLogger);
  // Before the body parser and the routes: a preflight carries no body and must
  // never reach a handler.
  app.use(cors());
  app.use(metricsMiddleware);
  // A resume template is the largest thing posted here; the default 100KB limit
  // rejects real LaTeX files with hand-written macros.
  app.use(express.json({ limit: "1mb" }));

  // ── Probes, before auth and before rate limiting ──
  app.get("/health", (_req, res) => {
    res.json({ status: "ok", service: "resumeforge-api" });
  });

  app.get("/ready", async (_req, res) => {
    // Readiness asks the one question that decides whether this process can
    // serve: is the database reachable? Redis is not included -- rate limiting
    // degrades, it does not stop the gateway working.
    try {
      await deps.prisma.$queryRaw`select 1`;
      res.json({ status: "ready", database: "ok", authMode: config.AUTH_MODE });
    } catch {
      res.status(503).json({ status: "degraded", database: "unreachable" });
    }
  });

  app.get("/metrics", async (_req, res) => {
    // Open, like the probes: a scraper cannot present a credential. The gateway
    // *is* internet-facing, so in a real deployment this path is blocked at the
    // reverse proxy -- see infra/aws/Caddyfile, which returns 403 for it.
    res.set("content-type", registry.contentType);
    res.send(await registry.metrics());
  });

  const readLimit = rateLimit(deps.redis, {
    name: "read",
    limit: config.RATE_LIMIT_READ_PER_MINUTE,
    windowSeconds: 60,
    // Fails open: a Redis outage must not become a total outage for reads.
    onFailure: "allow",
  });

  app.use("/api/auth", authRouter(deps.prisma));
  app.use("/api/profile", readLimit, profileRouter(deps.prisma));
  app.use("/api/sessions", readLimit, sessionsRouter(deps));

  app.use(notFoundHandler);
  app.use(errorHandler);
  return app;
}
