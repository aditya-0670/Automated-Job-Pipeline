/** Redis, treated as optional. It backs rate limiting only, and the gateway
 *  must start without it -- see the failure-mode split in middleware/rateLimit. */

import { Redis } from "ioredis";

import { getConfig } from "./config.js";
import { logger } from "./logger.js";

let client: Redis | null | undefined;

export function getRedis(): Redis | null {
  if (client !== undefined) return client;
  try {
    client = new Redis(getConfig().REDIS_URL, {
      // Fail fast and let the limiter decide: retrying forever inside the
      // client would turn a rate-limit check into a hung request.
      maxRetriesPerRequest: 1,
      enableOfflineQueue: false,
      lazyConnect: false,
    });
    client.on("error", (err) => logger.warn({ err: err.message }, "redis error"));
  } catch (err) {
    logger.warn({ err }, "Redis unavailable; rate limiting degraded");
    client = null;
  }
  return client;
}

export async function disconnectRedis(): Promise<void> {
  await client?.quit().catch(() => undefined);
  client = undefined;
}
