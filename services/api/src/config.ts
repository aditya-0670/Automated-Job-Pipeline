/**
 * Environment-driven configuration. No secret has a usable default.
 *
 * Validated at import time with zod, so a missing variable is a startup failure
 * with a readable message rather than `undefined` reaching a JWT verifier at
 * request time.
 */

import { z } from "zod";

const schema = z.object({
  NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
  API_PORT: z.coerce.number().int().positive().default(4000),
  LOG_LEVEL: z.string().default("info"),

  DATABASE_URL: z.string().min(1),
  REDIS_URL: z.string().default("redis://redis:6379"),

  AI_SERVICE_URL: z.string().default("http://ai:8000"),
  /** The gateway is the only holder of this. It is what makes the AI service
   *  trust us and nothing else. */
  INTERNAL_API_KEY: z.string().min(1),

  JWT_SECRET: z.string().min(8),
  /** Encrypts stored GitHub PATs. Separate from JWT_SECRET on purpose: one
   *  signs short-lived tokens and can be rotated freely, the other is the only
   *  thing standing between a database dump and a usable credential. Sharing
   *  them would make rotating the cheap one break the expensive one. */
  ENCRYPTION_KEY: z.string().min(16),
  /**
   * `dev` mints a token for the seeded user on request; `strict` does not.
   * Both modes verify tokens through exactly the same code path -- only the
   * issuing endpoint differs -- so dev mode cannot drift into a weaker check
   * than the one production runs.
   */
  AUTH_MODE: z.enum(["dev", "strict"]).default("dev"),
  /** Comma-separated origins allowed to call this API from a browser. */
  WEB_ORIGINS: z.string().default("http://localhost:3000"),
  SEED_USER_EMAIL: z.string().default("aditya@resumeforge.dev"),

  /** How long a GitHub sync is considered fresh. A repo list does not change
   *  minute to minute, and the point of the TTL is that a user mashing "sync"
   *  spends none of their 5,000/hour on it. */
  GITHUB_SYNC_TTL_MINUTES: z.coerce.number().int().positive().default(60),

  /** Cheap reads: a generous window, and it fails open. */
  RATE_LIMIT_READ_PER_MINUTE: z.coerce.number().int().positive().default(120),
  /** Pipeline runs: each one spends real LLM tokens, so this one fails closed. */
  RATE_LIMIT_RUN_PER_HOUR: z.coerce.number().int().positive().default(20),
});

export type Config = z.infer<typeof schema>;

let cached: Config | undefined;

export function getConfig(): Config {
  if (cached) return cached;
  const parsed = schema.safeParse(process.env);
  if (!parsed.success) {
    const issues = parsed.error.issues.map((i) => `  ${i.path.join(".")}: ${i.message}`).join("\n");
    throw new Error(`Invalid environment:\n${issues}`);
  }
  cached = parsed.data;
  return cached;
}

/** Test hook. Production never calls this. */
export function resetConfig(): void {
  cached = undefined;
}
