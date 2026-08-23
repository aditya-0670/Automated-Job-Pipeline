/**
 * The session flow: start a run, watch it, answer its gates, download the PDF.
 *
 * The gateway keeps its own `ChatSession` row alongside the pipeline's
 * checkpoint, and the two share one key -- the session id **is** the LangGraph
 * `thread_id`. That is what makes "list my past resumes" one indexed query
 * instead of a scan over checkpoint blobs, while the checkpoint stays the source
 * of truth for anything in flight.
 *
 * The row is written *before* the pipeline is started. A run that begins and
 * then fails to be recorded is a run the user is paying for and cannot find; the
 * reverse -- a row whose pipeline never started -- is visible, explainable, and
 * cleaned up on the next status read.
 */

import { Router } from "express";
import { z } from "zod";

import { getConfig } from "../config.js";
import { requireAuth, requireAuthAllowingQueryToken } from "../middleware/auth.js";
import { ApiError, asyncHandler, failMidStream } from "../middleware/errors.js";
import { rateLimit } from "../middleware/rateLimit.js";
import { pathParam, validate } from "../middleware/validate.js";
import { PROFILE_INCLUDE, toAiProfile, type ProfileRows } from "../profile.js";
import { logger } from "../logger.js";
import { recordStream } from "../metrics.js";

import type { AiClient } from "../services/aiClient.js";
import type { PrismaClient } from "@prisma/client";
import type { Redis } from "ioredis";

const createBody = z
  .object({
    jobUrl: z.string().url().optional(),
    jobText: z.string().min(200).optional(),
    maxIterations: z.number().int().min(1).max(5).optional(),
  })
  .refine((v) => Boolean(v.jobUrl || v.jobText), {
    message: "Provide jobUrl or jobText (at least 200 characters)",
  });

const reviewBody = z
  .object({
    decision: z.enum(["accept", "request_changes", "edit", "modify_keywords"]),
    changeRequest: z.string().min(3).optional(),
    editedLatex: z.string().min(20).optional(),
  })
  .refine((v) => v.decision !== "request_changes" || Boolean(v.changeRequest), {
    message: "request_changes needs changeRequest",
    path: ["changeRequest"],
  })
  .refine((v) => v.decision !== "edit" || Boolean(v.editedLatex), {
    message: "edit needs editedLatex",
    path: ["editedLatex"],
  });

const keywordsBody = z.object({
  keywords: z.array(z.object({ term: z.string().min(1) }).passthrough()).min(1).optional(),
});

const idParam = z.object({ id: z.string().min(1) });

export interface SessionDeps {
  prisma: PrismaClient;
  redis: Redis | null;
  /** Built per request so the correlation id travels with it. */
  aiClient: (requestId?: string) => AiClient;
}

export function sessionsRouter({ prisma, redis, aiClient }: SessionDeps): Router {
  const router = Router();
  const config = getConfig();

  /** Every session route resolves the session through the caller's id, so one
   *  user can never address another's session -- including on the SSE and PDF
   *  routes, which are the easy ones to forget. */
  async function ownedSession(userId: string, sessionId: string) {
    const session = await prisma.chatSession.findFirst({
      where: { id: sessionId, userId },
      select: { id: true, status: true },
    });
    if (!session) throw ApiError.notFound("Session not found");
    return session;
  }

  // ── Start ──
  router.post(
    "/",
    requireAuth,
    rateLimit(redis, {
      name: "pipeline-run",
      limit: config.RATE_LIMIT_RUN_PER_HOUR,
      windowSeconds: 3600,
      // Fails closed: a run spends model quota, and "we could not check, so we
      // allowed it" is how a broken Redis becomes an exhausted quota.
      onFailure: "deny",
    }),
    validate("body", createBody),
    asyncHandler(async (req, res) => {
      const body = req.body as z.infer<typeof createBody>;
      const user = await prisma.user.findUnique({
        where: { id: req.user!.id },
        include: PROFILE_INCLUDE as never,
      });
      if (!user) throw ApiError.notFound("Profile not found");

      const latexTemplate = (user as { latexTemplate?: string | null }).latexTemplate;
      if (!latexTemplate) {
        // A precondition, not an upstream failure: the pipeline would refuse
        // this too, but only after a scrape and an extraction have been paid for.
        throw ApiError.badRequest(
          "Upload your LaTeX resume template first (PUT /api/profile/latex).",
        );
      }

      const profile = toAiProfile(user as unknown as ProfileRows);
      if (profile.experiences.length + profile.projects.length === 0) {
        throw ApiError.badRequest(
          "Add at least one experience or project: the pipeline can only use evidence you have given it.",
        );
      }

      const session = await prisma.chatSession.create({
        data: {
          userId: req.user!.id,
          jobUrl: body.jobUrl ?? null,
          jobText: body.jobText ?? null,
          status: "running",
          currentStep: "INIT",
        },
      });

      try {
        await aiClient(req.id as string).run({
          // The row's id becomes the LangGraph thread_id: one key for both halves.
          sessionId: session.id,
          userId: req.user!.id,
          userLatex: latexTemplate,
          userProfile: profile,
          jobUrl: body.jobUrl,
          jobText: body.jobText,
          maxIterations: body.maxIterations,
        });
      } catch (err) {
        // The row exists but nothing is running behind it. Marked, not deleted:
        // the user asked for this and deserves to see what happened to it.
        await prisma.chatSession.update({
          where: { id: session.id },
          data: { status: "failed", error: err instanceof Error ? err.message : String(err) },
        });
        throw err;
      }

      res.status(202).json({
        sessionId: session.id,
        status: "running",
        streamUrl: `/api/sessions/${session.id}/stream`,
      });
    }),
  );

  // ── List and read ──
  router.get(
    "/",
    requireAuth,
    asyncHandler(async (req, res) => {
      const sessions = await prisma.chatSession.findMany({
        where: { userId: req.user!.id },
        orderBy: { createdAt: "desc" },
        take: 50,
        select: {
          id: true,
          jobUrl: true,
          jobTitle: true,
          company: true,
          status: true,
          currentStep: true,
          createdAt: true,
          completedAt: true,
        },
      });
      res.json({ sessions });
    }),
  );

  router.get(
    "/:id",
    requireAuth,
    validate("params", idParam),
    asyncHandler(async (req, res) => {
      await ownedSession(req.user!.id, pathParam(req, "id"));
      const includeDiff = req.query.includeDiff !== "false";
      const status = await aiClient(req.id as string).status(pathParam(req, "id"), { includeDiff });

      // Keep the denormalised copy honest on every read. Cheap, and it means the
      // session list never shows "running" for something that finished an hour
      // ago because no write happened to trigger an update.
      await syncSessionRow(prisma, pathParam(req, "id"), status);
      res.json(status);
    }),
  );

  // ── Answer the gates ──
  router.post(
    "/:id/keywords",
    requireAuth,
    validate("params", idParam),
    validate("body", keywordsBody),
    asyncHandler(async (req, res) => {
      await ownedSession(req.user!.id, pathParam(req, "id"));
      const { keywords } = req.body as z.infer<typeof keywordsBody>;
      const result = await aiClient(req.id as string).resumeKeywords(pathParam(req, "id"), keywords);
      res.status(202).json(result);
    }),
  );

  router.post(
    "/:id/review",
    requireAuth,
    validate("params", idParam),
    validate("body", reviewBody),
    asyncHandler(async (req, res) => {
      await ownedSession(req.user!.id, pathParam(req, "id"));
      const body = req.body as z.infer<typeof reviewBody>;
      const result = await aiClient(req.id as string).resumeReview(pathParam(req, "id"), {
        decision: body.decision,
        changeRequest: body.changeRequest,
        editedLatex: body.editedLatex,
      });
      res.status(202).json(result);
    }),
  );

  // ── Watch ──
  router.get(
    "/:id/stream",
    // The one route that accepts a token in the query string, because
    // EventSource cannot set an Authorization header.
    requireAuthAllowingQueryToken,
    validate("params", idParam),
    asyncHandler(async (req, res) => {
      await ownedSession(req.user!.id, pathParam(req, "id"));

      // Aborting the upstream request when the browser goes away is the whole
      // point: without it, every closed tab leaks a connection and a Postgres
      // poll on the AI service for as long as its stream cap allows.
      const upstream = new AbortController();
      recordStream("opened");
      req.on("close", () => {
        upstream.abort();
        // Paired with "opened" so a growing gap between the two is visible: that
        // gap is leaked upstream connections and Postgres polls.
        recordStream("closed");
      });

      // The header is what a reconnecting EventSource sends by itself; the query
      // parameter is how a client that opens a *new* stream says where it got
      // to, which the browser cannot express any other way. The header wins when
      // both are present, because only the browser sets it.
      const header = req.headers["last-event-id"];
      const fromHeader = Array.isArray(header) ? header[0] : header;
      const fromQuery = typeof req.query.lastEventId === "string" ? req.query.lastEventId : undefined;
      const response = await aiClient(req.id as string).events(pathParam(req, "id"), {
        lastEventId: fromHeader || fromQuery,
        signal: upstream.signal,
      });

      if (!response.ok || !response.body) {
        throw ApiError.upstream(`The event stream is unavailable (${response.status})`);
      }

      res.status(200);
      res.setHeader("content-type", "text/event-stream");
      res.setHeader("cache-control", "no-cache, no-transform");
      res.setHeader("connection", "keep-alive");
      // Nginx buffers text/event-stream by default, which turns a live stream
      // into one delivery at the end. This header is how you turn that off.
      res.setHeader("x-accel-buffering", "no");
      res.flushHeaders();

      try {
        // Bytes are forwarded untouched. Parsing and re-serialising the frames
        // here would put a second SSE implementation between the pipeline and
        // the browser, and the event ids the client resumes from are already
        // correct.
        for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
          res.write(chunk);
        }
      } catch (err) {
        if (!upstream.signal.aborted) {
          logger.warn({ err, sessionId: pathParam(req, "id") }, "event stream ended abnormally");
          failMidStream(res, "The event stream ended unexpectedly.");
          return;
        }
      }
      res.end();
    }),
  );

  // ── Download ──
  router.get(
    "/:id/pdf",
    requireAuthAllowingQueryToken,
    validate("params", idParam),
    asyncHandler(async (req, res) => {
      await ownedSession(req.user!.id, pathParam(req, "id"));
      const response = await aiClient(req.id as string).pdf(pathParam(req, "id"));

      if (!response.ok) {
        const detail = await response.text();
        // 409 and 410 mean something specific to the client (not yet / gone),
        // so they are forwarded rather than collapsed into a 502.
        const status = response.status >= 400 && response.status < 500 ? response.status : 502;
        throw new ApiError(status, "pdf_unavailable", detail || "The PDF is not available.");
      }

      res.setHeader("content-type", "application/pdf");
      res.setHeader("content-disposition", `attachment; filename="resume-${pathParam(req, "id")}.pdf"`);
      const buffer = Buffer.from(await response.arrayBuffer());
      res.send(buffer);
    }),
  );

  return router;
}

/** Mirror the pipeline's terminal state onto the session row. */
async function syncSessionRow(
  prisma: PrismaClient,
  sessionId: string,
  status: Record<string, unknown>,
): Promise<void> {
  const step = typeof status.step === "string" ? status.step : "INIT";
  const isComplete = status.is_complete === true;
  const failed = step === "FAILED";
  const review = status.human_review as { latex?: string } | undefined;

  await prisma.chatSession
    .update({
      where: { id: sessionId },
      data: {
        currentStep: step,
        status: failed ? "failed" : isComplete ? "completed" : "running",
        error: typeof status.error === "string" ? status.error : null,
        ...(review?.latex ? { generatedLatex: review.latex } : {}),
        ...(isComplete && !failed ? { completedAt: new Date() } : {}),
      },
    })
    .catch((err) => {
      // A failed mirror-write must not fail the read: the checkpoint is the
      // source of truth and the client already has the answer in hand.
      logger.warn({ err, sessionId }, "could not sync session row");
    });
}
