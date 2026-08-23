/** Server bootstrap: wire the real dependencies, listen, shut down cleanly. */

import { createApp } from "./app.js";
import { getConfig } from "./config.js";
import { disconnectPrisma, getPrisma } from "./db.js";
import { logger } from "./logger.js";
import { disconnectRedis, getRedis } from "./redis.js";
import { createAiClient } from "./services/aiClient.js";

const config = getConfig();
const prisma = getPrisma();
const redis = getRedis();

const app = createApp({ prisma, redis, aiClient: createAiClient });

const server = app.listen(config.API_PORT, () => {
  logger.info(
    {
      port: config.API_PORT,
      authMode: config.AUTH_MODE,
      aiService: config.AI_SERVICE_URL,
      rateLimits: {
        readPerMinute: config.RATE_LIMIT_READ_PER_MINUTE,
        runsPerHour: config.RATE_LIMIT_RUN_PER_HOUR,
      },
    },
    "API gateway listening",
  );
});

async function shutdown(signal: string): Promise<void> {
  logger.info({ signal }, "shutting down");
  // Stop accepting, then let in-flight requests finish. An open SSE stream would
  // otherwise hold this forever, so the process exits on a timer regardless --
  // the pipeline state is checkpointed, so a dropped stream costs a reconnect.
  const forced = setTimeout(() => process.exit(0), 10_000);
  server.close(async () => {
    clearTimeout(forced);
    await Promise.allSettled([disconnectPrisma(), disconnectRedis()]);
    process.exit(0);
  });
}

process.on("SIGTERM", () => void shutdown("SIGTERM"));
process.on("SIGINT", () => void shutdown("SIGINT"));
