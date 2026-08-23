/**
 * Redis fixed-window rate limiting, with a deliberate split on failure mode.
 *
 * **Reads fail open.** If Redis is down, a rate limiter that rejects turns a
 * cache outage into a total outage. The limit exists to stop accidental
 * hammering, and accidental hammering during an outage is the lesser problem.
 *
 * **Pipeline runs fail closed.** Each run spends real LLM tokens against a quota
 * that is 20 requests per day on the free tier. "We could not check the limit,
 * so we allowed it" is how a broken Redis becomes an exhausted quota and a bill.
 * Refusing with a clear message is recoverable; spending is not.
 */

import type { Redis } from "ioredis";
import type { RequestHandler } from "express";

import { logger } from "../logger.js";
import { recordRateLimited } from "../metrics.js";
import { ApiError } from "./errors.js";

export interface LimitOptions {
  name: string;
  limit: number;
  windowSeconds: number;
  /** What to do when Redis itself is unreachable. */
  onFailure: "allow" | "deny";
}

export function rateLimit(redis: Redis | null, options: LimitOptions): RequestHandler {
  return (req, res, next) => {
    if (!redis) {
      // No Redis configured at all (tests, single-node dev). Not a failure.
      next();
      return;
    }

    // Keyed by user when authenticated, by IP otherwise. Authenticated is the
    // meaningful unit: users share NATs, and one office should not be one bucket.
    const subject = req.user?.id ?? req.ip ?? "anonymous";
    const window = Math.floor(Date.now() / 1000 / options.windowSeconds);
    const key = `rl:${options.name}:${subject}:${window}`;

    redis
      .multi()
      .incr(key)
      // Set on every hit rather than only the first: a TTL lost to a failed
      // EXPIRE would leave the key immortal and the user permanently limited.
      .expire(key, options.windowSeconds)
      .exec()
      .then((results) => {
        const count = Number(results?.[0]?.[1] ?? 0);
        const remaining = Math.max(0, options.limit - count);
        res.setHeader("x-ratelimit-limit", options.limit);
        res.setHeader("x-ratelimit-remaining", remaining);
        if (count > options.limit) {
          recordRateLimited(options.name);
          next(
            ApiError.tooManyRequests(
              `Rate limit exceeded for ${options.name}. Try again later.`,
              options.windowSeconds,
            ),
          );
          return;
        }
        next();
      })
      .catch((err) => {
        logger.warn({ err, limiter: options.name }, "rate limit check failed");
        if (options.onFailure === "deny") {
          next(
            new ApiError(
              503,
              "rate_limit_unavailable",
              "Cannot verify the usage limit right now, and this action spends " +
                "model quota, so it was not started. Try again shortly.",
            ),
          );
          return;
        }
        next();
      });
  };
}
