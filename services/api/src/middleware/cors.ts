/**
 * CORS for the one origin that is allowed to call this API.
 *
 * An allowlist rather than `*`, and the reason is specific to how this API
 * authenticates. Bearer tokens live in the browser's `localStorage`, so with a
 * wildcard any page the user visits could read nothing directly — but any XSS
 * anywhere, or a malicious extension, could use a stolen token against the API
 * from its own origin. The allowlist means a token is only useful from a page we
 * served. It is not a substitute for the token; it narrows where the token works.
 *
 * `credentials` is deliberately *not* enabled: this API never uses cookies, and
 * `Access-Control-Allow-Credentials` with a reflected origin is the combination
 * that turns a permissive CORS policy into CSRF.
 */

import type { RequestHandler } from "express";

import { getConfig } from "../config.js";

const ALLOWED_HEADERS = ["content-type", "authorization", "last-event-id", "x-request-id"];
const ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"];

export function cors(): RequestHandler {
  const allowed = new Set(
    getConfig()
      .WEB_ORIGINS.split(",")
      .map((origin) => origin.trim())
      .filter(Boolean),
  );

  return (req, res, next) => {
    const origin = req.headers.origin;

    if (origin && allowed.has(origin)) {
      res.setHeader("access-control-allow-origin", origin);
      // Without this, a cache in front of the API can serve one origin's
      // response, complete with its allow-origin header, to another.
      res.setHeader("vary", "origin");
      res.setHeader("access-control-expose-headers", "x-request-id, x-ratelimit-remaining");
    }

    if (req.method === "OPTIONS") {
      res.setHeader("access-control-allow-methods", ALLOWED_METHODS.join(", "));
      res.setHeader("access-control-allow-headers", ALLOWED_HEADERS.join(", "));
      res.setHeader("access-control-max-age", "600");
      // 204 and stop: a preflight must not reach a route, least of all an
      // authenticated one, since it carries no credentials by design.
      res.status(204).end();
      return;
    }

    next();
  };
}
