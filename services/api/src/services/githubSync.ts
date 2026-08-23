/**
 * Turning GitHub repositories into profile projects.
 *
 * Separate from `github.ts` because that file knows about HTTP and this one
 * knows about the database, and the interesting rules are all here:
 *
 *   * **A synced project never overwrites a hand-written one.** Upserts key on
 *     `(userId, repoName)`, and manual projects have a null `repoName`, so the
 *     two sets cannot collide. Postgres treats NULLs as distinct in a unique
 *     index, which is what makes that work.
 *   * **A repository that disappears from GitHub is not deleted here.** The user
 *     may have edited its bullets; deleting is destructive and unasked for. It
 *     keeps its stale `lastSyncedAt`, which is visible and reversible.
 *   * **Bullets are never written by the sync.** They are the resume's prose,
 *     the user's or the Refactorer's. The sync owns evidence — description,
 *     languages, topics, README — and nothing that appears verbatim on a page.
 */

import { getConfig } from "../config.js";
import { decryptSecret } from "../crypto.js";
import { logger } from "../logger.js";
import { ApiError } from "../middleware/errors.js";
import { GitHubClient, isFresh, projectDetail, techFromLanguages } from "./github.js";

import type { PrismaClient } from "@prisma/client";

export interface SyncResult {
  status: "synced" | "fresh" | "unchanged";
  reposSeen: number;
  reposKept: number;
  created: number;
  updated: number;
  apiRequests: number;
  rateLimitRemaining: number | null;
  syncedAt: Date;
}

export interface SyncOptions {
  /** Skip the TTL check. The user pressing "sync now" means it. */
  force?: boolean;
  fetchImpl?: typeof fetch;
  now?: Date;
}

export async function syncGitHubProjects(
  prisma: PrismaClient,
  userId: string,
  options: SyncOptions = {},
): Promise<SyncResult> {
  const config = getConfig();
  const now = options.now ?? new Date();

  const user = await prisma.user.findUnique({
    where: { id: userId },
    select: { githubToken: true, githubReposEtag: true, githubSyncedAt: true },
  });
  if (!user) throw ApiError.notFound("Profile not found");
  if (!user.githubToken) {
    throw ApiError.badRequest("Add a GitHub token first (PUT /api/profile/github/token).");
  }

  // The cheapest possible answer: no token decryption, no client, no network.
  if (!options.force && isFresh(user.githubSyncedAt, config.GITHUB_SYNC_TTL_MINUTES, now)) {
    logger.info({ userId }, "GitHub sync skipped: still fresh");
    return {
      status: "fresh",
      reposSeen: 0,
      reposKept: 0,
      created: 0,
      updated: 0,
      apiRequests: 0,
      rateLimitRemaining: null,
      syncedAt: user.githubSyncedAt!,
    };
  }

  let token: string;
  try {
    token = decryptSecret(user.githubToken);
  } catch (err) {
    // The key changed, or the row was tampered with. Either way the stored value
    // is unusable and saying so is better than sending garbage to GitHub as a
    // credential and reporting a 401.
    logger.error({ err, userId }, "stored GitHub token could not be decrypted");
    throw ApiError.badRequest("Your stored GitHub token could not be read. Re-add it.");
  }

  const client = new GitHubClient(token, options.fetchImpl);
  const listing = await client.listRepos(user.githubReposEtag);

  if (listing.repos === null) {
    // 304: GitHub charged nothing for this. Only the timestamp moves, so the TTL
    // window restarts and the next sync is free too.
    await prisma.user.update({ where: { id: userId }, data: { githubSyncedAt: now } });
    logger.info({ userId, requests: listing.requests }, "GitHub sync: nothing changed (304)");
    return {
      status: "unchanged",
      reposSeen: 0,
      reposKept: 0,
      created: 0,
      updated: 0,
      apiRequests: listing.requests,
      rateLimitRemaining: listing.rateLimitRemaining,
      syncedAt: now,
    };
  }

  const projects = await client.enrich(listing.repos);

  let created = 0;
  let updated = 0;
  for (const project of projects) {
    const data = {
      name: project.repoName,
      description: project.description,
      tech: techFromLanguages(project.languages),
      detail: projectDetail(project),
      repoName: project.repoName,
      repoUrl: project.repoUrl,
      stars: project.stars,
      languages: project.languages,
      topics: project.topics,
      readme: project.readme,
      source: "github",
      lastSyncedAt: now,
      startDate: project.createdAt,
      // A repository pushed to in the last 90 days reads as current work; older
      // than that and dating it "present" on a resume is a small untruth.
      endDate: now.getTime() - project.pushedAt.getTime() > 90 * 86_400_000 ? project.pushedAt : null,
    };

    const result = await prisma.project.upsert({
      where: { userId_repoName: { userId, repoName: project.repoName } },
      // `bullets` is absent from both branches on purpose: the sync must never
      // touch the prose that appears on the resume.
      create: { userId, ...data, bullets: [] },
      update: data,
    });
    if (result.createdAt.getTime() === result.updatedAt.getTime()) created += 1;
    else updated += 1;
  }

  await prisma.user.update({
    where: { id: userId },
    data: { githubSyncedAt: now, githubReposEtag: listing.etag },
  });

  logger.info(
    {
      userId,
      seen: listing.repos.length,
      kept: projects.length,
      created,
      updated,
      requests: client.requestCount,
    },
    "GitHub sync complete",
  );

  return {
    status: "synced",
    reposSeen: listing.repos.length,
    reposKept: projects.length,
    created,
    updated,
    apiRequests: client.requestCount,
    rateLimitRemaining: listing.rateLimitRemaining,
    syncedAt: now,
  };
}
