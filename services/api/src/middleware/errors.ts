/**
 * One error shape for the whole API, and one place that decides status codes.
 *
 * Clients parse errors. If half the routes return `{error: "..."}` and half
 * return `{message: "..."}`, every caller grows a branch, so the envelope is
 * fixed here and thrown from anywhere via `ApiError`.
 */

import type { ErrorRequestHandler, NextFunction, Request, RequestHandler, Response } from "express";

import { logger } from "../logger.js";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }

  static badRequest(message: string, details?: unknown) {
    return new ApiError(400, "bad_request", message, details);
  }
  static unauthorized(message = "Authentication required") {
    return new ApiError(401, "unauthorized", message);
  }
  static forbidden(message = "Not your resource") {
    return new ApiError(403, "forbidden", message);
  }
  static notFound(message: string) {
    return new ApiError(404, "not_found", message);
  }
  static conflict(message: string) {
    return new ApiError(409, "conflict", message);
  }
  static tooManyRequests(message: string, retryAfterSeconds: number) {
    return new ApiError(429, "rate_limited", message, { retryAfterSeconds });
  }
  static upstream(message: string, status = 502, details?: unknown) {
    return new ApiError(status, "upstream_error", message, details);
  }
}

/**
 * Express 5 forwards a rejected promise to the error handler on its own, but
 * only for handlers it recognises as async. Wrapping is still the honest way to
 * say "this one can reject", and it keeps the routes free of try/catch.
 */
export function asyncHandler(
  fn: (req: Request, res: Response, next: NextFunction) => Promise<unknown>,
): RequestHandler {
  return (req, res, next) => {
    fn(req, res, next).catch(next);
  };
}

export const notFoundHandler: RequestHandler = (req) => {
  throw ApiError.notFound(`No route for ${req.method} ${req.path}`);
};

export const errorHandler: ErrorRequestHandler = (err, req, res, _next) => {
  const requestId = (req as { id?: string }).id;

  if (err instanceof ApiError) {
    if (err.status >= 500) logger.error({ err, requestId }, "request failed");
    res.status(err.status).json({
      error: { code: err.code, message: err.message, details: err.details, requestId },
    });
    return;
  }

  // Anything else is a bug. The message is deliberately not forwarded: an
  // unexpected exception's text is written for a developer and regularly
  // contains connection strings, file paths or fragments of a query.
  logger.error({ err, requestId }, "unhandled error");
  res.status(500).json({
    error: { code: "internal_error", message: "Something went wrong.", requestId },
  });
};

/** A response already streaming cannot be given a status code. */
export function failMidStream(res: Response, message: string): void {
  if (res.headersSent) {
    res.write(`event: error\ndata: ${JSON.stringify({ message })}\n\n`);
    res.end();
  }
}
