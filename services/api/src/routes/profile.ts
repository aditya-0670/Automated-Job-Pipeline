/**
 * Profile CRUD. The data the pipeline draws its evidence from.
 *
 * Every route is scoped to `req.user.id` in the *where clause*, never by
 * checking ownership after the fetch. A `findUnique` by id followed by an
 * ownership check is one forgotten check away from an IDOR; a `where` that
 * always includes the user id cannot be forgotten silently -- the row simply is
 * not found.
 */

import { Router } from "express";
import { z } from "zod";

import { requireAuth } from "../middleware/auth.js";
import { ApiError, asyncHandler } from "../middleware/errors.js";
import { pathParam, validate } from "../middleware/validate.js";
import { PROFILE_INCLUDE, toAiProfile, type ProfileRows } from "../profile.js";
import { encryptSecret } from "../crypto.js";
import { syncGitHubProjects } from "../services/githubSync.js";
import { getConfig } from "../config.js";
import { isFresh } from "../services/github.js";

import type { PrismaClient } from "@prisma/client";

const monthDate = z
  .string()
  .regex(/^\d{4}-\d{2}$/, "Expected YYYY-MM")
  .transform((value) => {
    const [year, month] = value.split("-").map(Number);
    return new Date(Date.UTC(year!, month! - 1, 1));
  });

const experienceBody = z.object({
  company: z.string().min(1),
  role: z.string().min(1),
  location: z.string().optional(),
  start: monthDate,
  end: monthDate.nullish(),
  bullets: z.array(z.string().min(1)).default([]),
  detail: z.string().optional(),
});

const projectBody = z.object({
  name: z.string().min(1),
  tech: z.array(z.string()).default([]),
  bullets: z.array(z.string()).default([]),
  detail: z.string().optional(),
  start: monthDate.nullish(),
  end: monthDate.nullish(),
});

const skillBody = z.object({
  name: z.string().min(1),
  category: z.string().min(1),
  proficiency: z.string().optional(),
});

const latexBody = z.object({
  // A resume template is a few KB. The ceiling is here because this string is
  // forwarded into an LLM prompt and a compiler, and both have limits that are
  // more expensive to discover downstream.
  latexTemplate: z.string().min(20).max(200_000),
});

const idParam = z.object({ id: z.string().min(1) });

const githubTokenBody = z.object({
  // Length only. Validating the prefix (`ghp_`, `github_pat_`) would reject
  // formats GitHub has not invented yet, and the authoritative check is the
  // first API call.
  token: z.string().min(20).max(500),
  username: z.string().min(1).max(100).optional(),
});

const syncQuery = z.object({
  // Coerced from a query string, so `?force=true` works and `?force` does not
  // silently mean false.
  force: z
    .union([z.literal("true"), z.literal("false"), z.boolean()])
    .default(false)
    .transform((v) => v === true || v === "true"),
});

export function profileRouter(prisma: PrismaClient): Router {
  const router = Router();
  router.use(requireAuth);

  /** The whole profile, in the shape the pipeline consumes. Also the endpoint a
   *  user can read to see exactly what the AI is allowed to draw on. */
  router.get(
    "/",
    asyncHandler(async (req, res) => {
      const user = await prisma.user.findUnique({
        where: { id: req.user!.id },
        include: PROFILE_INCLUDE as never,
      });
      if (!user) throw ApiError.notFound("Profile not found");
      res.json({
        profile: toAiProfile(user as unknown as ProfileRows),
        hasLatexTemplate: Boolean((user as { latexTemplate?: string }).latexTemplate),
      });
    }),
  );

  router.put(
    "/latex",
    validate("body", latexBody),
    asyncHandler(async (req, res) => {
      const { latexTemplate } = req.body as z.infer<typeof latexBody>;
      await prisma.user.update({ where: { id: req.user!.id }, data: { latexTemplate } });
      res.json({ ok: true, bytes: latexTemplate.length });
    }),
  );

  router.get(
    "/latex",
    asyncHandler(async (req, res) => {
      const user = await prisma.user.findUnique({
        where: { id: req.user!.id },
        select: { latexTemplate: true },
      });
      if (!user?.latexTemplate) throw ApiError.notFound("No LaTeX template uploaded yet");
      res.type("text/plain").send(user.latexTemplate);
    }),
  );

  // ── Experiences ──
  router.post(
    "/experiences",
    validate("body", experienceBody),
    asyncHandler(async (req, res) => {
      const body = req.body as z.infer<typeof experienceBody>;
      const created = await prisma.workExperience.create({
        data: {
          userId: req.user!.id,
          company: body.company,
          role: body.role,
          location: body.location ?? null,
          startDate: body.start,
          endDate: body.end ?? null,
          bullets: body.bullets,
          detail: body.detail ?? null,
        },
      });
      res.status(201).json(created);
    }),
  );

  router.patch(
    "/experiences/:id",
    validate("params", idParam),
    validate("body", experienceBody.partial()),
    asyncHandler(async (req, res) => {
      const body = req.body as Partial<z.infer<typeof experienceBody>>;
      // updateMany, not update: it takes a filter, so the user id is part of the
      // query rather than a check performed afterwards.
      const result = await prisma.workExperience.updateMany({
        where: { id: pathParam(req, "id"), userId: req.user!.id },
        data: {
          ...(body.company !== undefined && { company: body.company }),
          ...(body.role !== undefined && { role: body.role }),
          ...(body.location !== undefined && { location: body.location }),
          ...(body.start !== undefined && { startDate: body.start }),
          ...(body.end !== undefined && { endDate: body.end }),
          ...(body.bullets !== undefined && { bullets: body.bullets }),
          ...(body.detail !== undefined && { detail: body.detail }),
        },
      });
      if (result.count === 0) throw ApiError.notFound("Experience not found");
      res.json({ ok: true });
    }),
  );

  router.delete(
    "/experiences/:id",
    validate("params", idParam),
    asyncHandler(async (req, res) => {
      const result = await prisma.workExperience.deleteMany({
        where: { id: pathParam(req, "id"), userId: req.user!.id },
      });
      if (result.count === 0) throw ApiError.notFound("Experience not found");
      res.status(204).end();
    }),
  );

  // ── Projects ──
  router.post(
    "/projects",
    validate("body", projectBody),
    asyncHandler(async (req, res) => {
      const body = req.body as z.infer<typeof projectBody>;
      const created = await prisma.project.create({
        data: {
          userId: req.user!.id,
          name: body.name,
          tech: body.tech,
          bullets: body.bullets,
          detail: body.detail ?? null,
          startDate: body.start ?? null,
          endDate: body.end ?? null,
          source: "manual",
        },
      });
      res.status(201).json(created);
    }),
  );

  router.delete(
    "/projects/:id",
    validate("params", idParam),
    asyncHandler(async (req, res) => {
      const result = await prisma.project.deleteMany({
        where: { id: pathParam(req, "id"), userId: req.user!.id },
      });
      if (result.count === 0) throw ApiError.notFound("Project not found");
      res.status(204).end();
    }),
  );

  // ── Skills ──
  router.put(
    "/skills",
    validate("body", z.object({ skills: z.array(skillBody).max(200) })),
    asyncHandler(async (req, res) => {
      const { skills } = req.body as { skills: z.infer<typeof skillBody>[] };
      // Replace as one transaction: a half-written skill list is a resume that
      // claims a subset of the truth, and the pipeline would treat the gap as a
      // genuine absence of evidence.
      const [, created] = await prisma.$transaction([
        prisma.skill.deleteMany({ where: { userId: req.user!.id } }),
        prisma.skill.createMany({
          data: skills.map((s) => ({
            userId: req.user!.id,
            name: s.name,
            category: s.category,
            proficiency: s.proficiency ?? null,
          })),
        }),
      ]);
      res.json({ ok: true, count: created.count });
    }),
  );

  // ── GitHub sync (Part 14) ──
  router.put(
    "/github/token",
    validate("body", githubTokenBody),
    asyncHandler(async (req, res) => {
      const { token, username } = req.body as z.infer<typeof githubTokenBody>;
      await prisma.user.update({
        where: { id: req.user!.id },
        data: {
          // Encrypted before it reaches the column; the plaintext exists only
          // for the length of this request.
          githubToken: encryptSecret(token),
          githubUsername: username ?? null,
          // A new token invalidates the conditional-request state: it may see a
          // different set of repositories, and a 304 against the old ETag would
          // report "nothing changed" about a list we have never fetched.
          githubReposEtag: null,
          githubSyncedAt: null,
        },
      });
      res.json({ ok: true });
    }),
  );

  router.delete(
    "/github/token",
    asyncHandler(async (req, res) => {
      await prisma.user.update({
        where: { id: req.user!.id },
        data: { githubToken: null, githubReposEtag: null, githubSyncedAt: null },
      });
      // Synced projects are left in place. They are the user's work; revoking a
      // token is not a request to delete their portfolio.
      res.json({ ok: true, note: "Synced projects were kept." });
    }),
  );

  router.get(
    "/github",
    asyncHandler(async (req, res) => {
      const user = await prisma.user.findUniqueOrThrow({
        where: { id: req.user!.id },
        select: { githubUsername: true, githubToken: true, githubSyncedAt: true },
      });
      const count = await prisma.project.count({
        where: { userId: req.user!.id, source: "github" },
      });
      res.json({
        // Never the token, not even its length.
        connected: Boolean(user.githubToken),
        username: user.githubUsername,
        lastSyncedAt: user.githubSyncedAt,
        fresh: isFresh(user.githubSyncedAt, getConfig().GITHUB_SYNC_TTL_MINUTES),
        syncedProjects: count,
      });
    }),
  );

  router.post(
    "/github/sync",
    validate("query", syncQuery),
    asyncHandler(async (req, res) => {
      const { force } = req.query as unknown as z.infer<typeof syncQuery>;
      const result = await syncGitHubProjects(prisma, req.user!.id, { force });
      res.json(result);
    }),
  );

  return router;
}
