/**
 * JWT authentication, with a dev issuer behind the same verifier.
 *
 * The temptation in a single-user MVP is a middleware that says "if AUTH_MODE is
 * dev, attach the seeded user". That is the wrong shape: the dev path then never
 * exercises verification, and the first real token in production meets code that
 * has never run. So dev mode changes only *who can mint* a token
 * (`POST /api/auth/dev-token`); every request is verified identically in both
 * modes, and Google OAuth later replaces the issuer, not this file.
 */

import jwt from "jsonwebtoken";

import { getConfig } from "../config.js";
import { ApiError } from "./errors.js";

import type { RequestHandler } from "express";

export interface AuthUser {
  id: string;
  email: string;
}

declare global {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace Express {
    interface Request {
      user?: AuthUser;
    }
  }
}

const ISSUER = "resumeforge";
const TOKEN_TTL_SECONDS = 60 * 60 * 12;

export function signToken(user: AuthUser): string {
  const { JWT_SECRET } = getConfig();
  return jwt.sign({ sub: user.id, email: user.email }, JWT_SECRET, {
    issuer: ISSUER,
    audience: ISSUER,
    expiresIn: TOKEN_TTL_SECONDS,
  });
}

/**
 * Verify and attach the caller.
 *
 * `issuer` and `audience` are asserted, not merely the signature: a token signed
 * with the same secret for a different purpose (a download link, a webhook)
 * would otherwise authenticate a session here.
 */
export const requireAuth: RequestHandler = (req, _res, next) => {
  const header = req.headers.authorization ?? "";
  const [scheme, token] = header.split(" ");
  if (scheme?.toLowerCase() !== "bearer" || !token) {
    next(ApiError.unauthorized("Send a bearer token in the Authorization header"));
    return;
  }

  try {
    const payload = jwt.verify(token, getConfig().JWT_SECRET, {
      issuer: ISSUER,
      audience: ISSUER,
    });
    if (typeof payload === "string" || !payload.sub) {
      next(ApiError.unauthorized("Malformed token"));
      return;
    }
    req.user = { id: String(payload.sub), email: String(payload.email ?? "") };
    next();
  } catch (err) {
    const expired = err instanceof jwt.TokenExpiredError;
    // Distinguished on purpose: "expired" tells a client to refresh, "invalid"
    // tells it to stop retrying. Collapsing them produces retry loops.
    next(ApiError.unauthorized(expired ? "Token expired" : "Invalid token"));
  }
};

/**
 * EventSource cannot set headers, so the browser cannot send a bearer token on
 * an SSE connection. The token therefore arrives as a query parameter on that
 * one route -- deliberately, and only there, because query strings land in
 * access logs. Everything else requires the header.
 */
export const requireAuthAllowingQueryToken: RequestHandler = (req, res, next) => {
  const queryToken = typeof req.query.token === "string" ? req.query.token : undefined;
  if (!req.headers.authorization && queryToken) {
    req.headers.authorization = `Bearer ${queryToken}`;
  }
  requireAuth(req, res, next);
};
