/**
 * zod at the edge, so no handler works with an unvalidated body.
 *
 * The parsed value replaces the raw one, which matters more than the check: a
 * handler reading `req.body` gets the coerced, typed object, so there is no
 * second place where a string might still be a string.
 */

import type { RequestHandler } from "express";
import type { ZodType } from "zod";

import { ApiError } from "./errors.js";

type Part = "body" | "query" | "params";

export function validate(part: Part, schema: ZodType): RequestHandler {
  return (req, _res, next) => {
    const result = schema.safeParse(req[part]);
    if (!result.success) {
      next(
        ApiError.badRequest(
          `Invalid request ${part}`,
          result.error.issues.map((i) => ({ path: i.path.join("."), message: i.message })),
        ),
      );
      return;
    }
    // Express 5 makes req.query a getter, so it is assigned via defineProperty
    // rather than plain assignment, which would silently do nothing.
    Object.defineProperty(req, part, { value: result.data, writable: true, configurable: true });
    next();
  };
}

/**
 * Read a validated path parameter as a string.
 *
 * Express 5 types `req.params` loosely (`string | string[] | undefined`) because
 * a wildcard route can produce an array. Every route here validates its params
 * with zod first, so this is a narrowing at the type level rather than a check
 * -- but it throws rather than casting, because a silent cast is how a route
 * added later without validation becomes an `undefined` in a where clause.
 */
export function pathParam(req: { params: Record<string, unknown> }, name: string): string {
  const value = req.params[name];
  if (typeof value !== "string" || value.length === 0) {
    throw ApiError.badRequest(`Missing path parameter: ${name}`);
  }
  return value;
}
