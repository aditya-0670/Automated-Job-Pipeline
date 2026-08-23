/**
 * Part 14: GitHub sync.
 *
 * A fake `fetch` rather than a mocked module, so the tests drive the HTTP
 * semantics the feature actually depends on -- 304s, 401s, 403 with a spent rate
 * limit, a missing README -- and every one of them counts requests. The whole
 * point of the feature is not making requests, so the assertion that matters
 * most is a number: zero.
 */

import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

process.env.INTERNAL_API_KEY ??= "test-internal-key";
process.env.JWT_SECRET ??= "test-jwt-secret-value";
process.env.ENCRYPTION_KEY ??= "test-encryption-key-32-bytes-min";
process.env.LOG_LEVEL = "silent";

const { PrismaClient } = await import("@prisma/client");
const { encryptSecret, decryptSecret } = await import("../src/crypto.js");
const { GitHubClient, isFresh, projectDetail, techFromLanguages } = await import(
  "../src/services/github.js"
);
const { syncGitHubProjects } = await import("../src/services/githubSync.js");

const hasDatabase = Boolean(process.env.DATABASE_URL);
const prisma = new PrismaClient();

// ── A fake GitHub ──────────────────────────────────────────────────────────
interface FakeRepo {
  name: string;
  fork?: boolean;
  archived?: boolean;
  private?: boolean;
  stars?: number;
  topics?: string[];
  pushedAt?: string;
  languages?: Record<string, number>;
  readme?: string | null;
}

function fakeGitHub(repos: FakeRepo[], options: { etag?: string; unchanged?: boolean } = {}) {
  const calls: string[] = [];

  const impl = (async (url: string | URL, init?: RequestInit) => {
    const path = String(url).replace("https://api.github.com", "");
    calls.push(path);
    const headers = (init?.headers ?? {}) as Record<string, string>;

    if (path.startsWith("/user/repos")) {
      if (options.unchanged && headers["if-none-match"]) {
        // GitHub does not charge a 304 against the rate limit.
        return new Response(null, {
          status: 304,
          headers: { "x-ratelimit-remaining": "4999" },
        });
      }
      const body = repos.map((r) => ({
        name: r.name,
        full_name: `aditya/${r.name}`,
        description: `${r.name} description`,
        html_url: `https://github.com/aditya/${r.name}`,
        fork: r.fork ?? false,
        archived: r.archived ?? false,
        private: r.private ?? false,
        stargazers_count: r.stars ?? 0,
        topics: r.topics ?? [],
        language: null,
        pushed_at: r.pushedAt ?? new Date().toISOString(),
        created_at: "2025-01-01T00:00:00Z",
      }));
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: {
          etag: options.etag ?? 'W/"abc123"',
          "x-ratelimit-remaining": "4990",
          "content-type": "application/json",
        },
      });
    }

    // /repos/{owner}/{repo}/... -- the repo is segment 3, not 2.
    const repoName = path.split("/")[3];
    const repo = repos.find((r) => r.name === repoName);

    if (path.endsWith("/languages")) {
      return new Response(JSON.stringify(repo?.languages ?? {}), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (path.endsWith("/readme")) {
      if (repo?.readme === null || repo?.readme === undefined) {
        // The normal answer for a repo with no README.
        return new Response("Not Found", { status: 404 });
      }
      return new Response(repo.readme, { status: 200 });
    }
    return new Response("unexpected", { status: 500 });
  }) as unknown as typeof fetch;

  return { impl, calls };
}

// ── Unit: no database needed ───────────────────────────────────────────────
describe("token encryption", () => {
  it("round trips", () => {
    const token = "ghp_averyrealisticlookingtoken1234";
    expect(decryptSecret(encryptSecret(token))).toBe(token);
  });

  it("produces different ciphertext each time", () => {
    // A deterministic ciphertext leaks equality: it would tell an attacker with
    // the dump which users share a token.
    const token = "ghp_averyrealisticlookingtoken1234";
    expect(encryptSecret(token)).not.toBe(encryptSecret(token));
  });

  it("refuses tampered ciphertext instead of returning garbage", () => {
    // This is why GCM and not CBC: a flipped byte must fail, not decrypt into
    // something that gets sent to GitHub as a credential.
    const encrypted = encryptSecret("ghp_token");
    const parts = encrypted.split(".");
    const data = Buffer.from(parts[3]!, "base64url");
    data[0] = (data[0]! ^ 0xff) & 0xff;
    parts[3] = data.toString("base64url");
    expect(() => decryptSecret(parts.join("."))).toThrow();
  });

  it("refuses a value in an unknown format", () => {
    // The version prefix exists so a future scheme is distinguishable rather
    // than guessed at.
    expect(() => decryptSecret("not-encrypted-at-all")).toThrow(/Unrecognised/);
  });
});

describe("freshness", () => {
  const now = new Date("2026-08-22T12:00:00Z");

  it("is stale when never synced", () => {
    expect(isFresh(null, 60, now)).toBe(false);
  });

  it("is fresh inside the window and stale outside it", () => {
    expect(isFresh(new Date("2026-08-22T11:30:00Z"), 60, now)).toBe(true);
    expect(isFresh(new Date("2026-08-22T10:59:00Z"), 60, now)).toBe(false);
  });
});

describe("evidence assembly", () => {
  it("orders languages by bytes, not alphabetically", () => {
    // The resume reads the first few; "most of this project" beats "starts with C".
    expect(techFromLanguages({ Python: 100, TypeScript: 900, C: 500 })).toEqual([
      "TypeScript",
      "C",
      "Python",
    ]);
  });

  it("combines description, languages, topics and README into one evidence blob", () => {
    const detail = projectDetail({
      repoName: "r",
      repoUrl: "u",
      description: "A parallel encryption engine",
      stars: 3,
      topics: ["cpp", "concurrency"],
      languages: { "C++": 900 },
      readme: "Uses POSIX threads and mmap.",
      pushedAt: new Date(),
      createdAt: new Date(),
    });
    expect(detail).toContain("parallel encryption engine");
    expect(detail).toContain("Languages: C++");
    expect(detail).toContain("Topics: cpp, concurrency");
    expect(detail).toContain("POSIX threads");
  });
});

describe("the client", () => {
  it("drops forks and archived repos before paying for their details", async () => {
    const github = fakeGitHub([
      { name: "real-project", languages: { "C++": 10 }, readme: "hi" },
      { name: "somebody-elses", fork: true },
      { name: "old-thing", archived: true },
    ]);
    const client = new GitHubClient("token", github.impl);
    const listing = await client.listRepos(null);
    const projects = await client.enrich(listing.repos!);

    expect(projects.map((p) => p.repoName)).toEqual(["real-project"]);
    // Two API calls each are never spent on the fork or the archived repo.
    expect(github.calls.filter((c) => c.includes("somebody-elses"))).toEqual([]);
    expect(github.calls.filter((c) => c.includes("old-thing"))).toEqual([]);
  });

  it("does not read a private repo's README", async () => {
    // The point of enrichment is public evidence, and a private README is the
    // likeliest place for something the user did not mean to publish.
    const github = fakeGitHub([{ name: "secret", private: true, readme: "internal notes" }]);
    const client = new GitHubClient("token", github.impl);
    const projects = await client.enrich((await client.listRepos(null)).repos!);
    expect(projects[0]!.readme).toBeNull();
    expect(github.calls.some((c) => c.endsWith("/readme"))).toBe(false);
  });

  it("treats a missing README as normal, not as a failure", async () => {
    const github = fakeGitHub([{ name: "no-readme", readme: null }]);
    const client = new GitHubClient("token", github.impl);
    const projects = await client.enrich((await client.listRepos(null)).repos!);
    expect(projects[0]!.readme).toBeNull();
  });

  it("reports a rejected token as the user's problem", async () => {
    const impl = (async () => new Response("Bad credentials", { status: 401 })) as typeof fetch;
    const client = new GitHubClient("revoked", impl);
    await expect(client.listRepos(null)).rejects.toMatchObject({
      status: 400,
      code: "github_unauthorized",
    });
  });

  it("distinguishes an exhausted rate limit from a plain refusal", async () => {
    const spent = (async () =>
      new Response("rate limited", {
        status: 403,
        headers: { "x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790000000" },
      })) as typeof fetch;
    await expect(new GitHubClient("t", spent).listRepos(null)).rejects.toMatchObject({
      status: 429,
      code: "github_rate_limited",
    });

    const forbidden = (async () =>
      new Response("nope", {
        status: 403,
        headers: { "x-ratelimit-remaining": "4000" },
      })) as typeof fetch;
    await expect(new GitHubClient("t", forbidden).listRepos(null)).rejects.toMatchObject({
      status: 403,
    });
  });

  it("turns an unreachable GitHub into a 502, not a 500", async () => {
    const dead = (async () => {
      throw new Error("ECONNREFUSED");
    }) as typeof fetch;
    await expect(new GitHubClient("t", dead).listRepos(null)).rejects.toMatchObject({
      status: 502,
    });
  });
});

// ── The sync, against the real database ────────────────────────────────────
describe.skipIf(!hasDatabase)("syncing to the profile", () => {
  let userId: string;

  beforeAll(async () => {
    const user = await prisma.user.upsert({
      where: { email: "github-sync-test@resumeforge.dev" },
      update: {},
      create: { email: "github-sync-test@resumeforge.dev", name: "Sync Test" },
    });
    userId = user.id;
  });

  beforeEach(async () => {
    await prisma.project.deleteMany({ where: { userId } });
    await prisma.user.update({
      where: { id: userId },
      data: {
        githubToken: encryptSecret("ghp_faketoken1234567890"),
        githubReposEtag: null,
        githubSyncedAt: null,
      },
    });
  });

  afterAll(async () => {
    await prisma.user.deleteMany({ where: { email: "github-sync-test@resumeforge.dev" } });
    await prisma.$disconnect();
  });

  it("creates projects from repositories", async () => {
    const github = fakeGitHub([
      { name: "resumeforge", languages: { Python: 900, TypeScript: 100 }, readme: "LangGraph pipeline", topics: ["ai"] },
      { name: "encryption-engine", languages: { "C++": 500 }, readme: "POSIX threads" },
    ]);
    const result = await syncGitHubProjects(prisma, userId, { fetchImpl: github.impl });

    expect(result.status).toBe("synced");
    expect(result.created).toBe(2);
    const projects = await prisma.project.findMany({ where: { userId }, orderBy: { name: "asc" } });
    expect(projects.map((p) => p.name)).toEqual(["encryption-engine", "resumeforge"]);
    // Languages become the tech list, ordered by bytes.
    expect(projects[1]!.tech).toEqual(["Python", "TypeScript"]);
    expect(projects[1]!.detail).toContain("LangGraph pipeline");
    expect(projects[1]!.source).toBe("github");
  });

  it("performs no API call at all on a second sync inside the TTL", async () => {
    // Part 14's acceptance criterion, and the reason the TTL is checked before
    // the token is even decrypted.
    const first = fakeGitHub([{ name: "a", languages: { Go: 1 } }]);
    await syncGitHubProjects(prisma, userId, { fetchImpl: first.impl });

    const second = fakeGitHub([{ name: "a", languages: { Go: 1 } }]);
    const result = await syncGitHubProjects(prisma, userId, { fetchImpl: second.impl });

    expect(result.status).toBe("fresh");
    expect(result.apiRequests).toBe(0);
    expect(second.calls).toEqual([]);
  });

  it("uses the stored ETag so an unchanged list costs one free request", async () => {
    const first = fakeGitHub([{ name: "a", languages: { Go: 1 } }], { etag: 'W/"v1"' });
    await syncGitHubProjects(prisma, userId, { fetchImpl: first.impl });

    // Forced past the TTL, so it really does ask GitHub -- and GitHub answers
    // 304, which is not charged against the rate limit.
    const again = fakeGitHub([{ name: "a" }], { unchanged: true });
    const result = await syncGitHubProjects(prisma, userId, {
      fetchImpl: again.impl,
      force: true,
    });

    expect(result.status).toBe("unchanged");
    expect(again.calls).toHaveLength(1);
    expect(again.calls[0]).toContain("/user/repos");
  });

  it("updates an existing synced project rather than duplicating it", async () => {
    const first = fakeGitHub([{ name: "a", stars: 1, languages: { Go: 1 } }]);
    await syncGitHubProjects(prisma, userId, { fetchImpl: first.impl });

    const second = fakeGitHub([{ name: "a", stars: 42, languages: { Go: 1 } }]);
    const result = await syncGitHubProjects(prisma, userId, {
      fetchImpl: second.impl,
      force: true,
    });

    expect(result.updated).toBe(1);
    const projects = await prisma.project.findMany({ where: { userId } });
    expect(projects).toHaveLength(1);
    expect(projects[0]!.stars).toBe(42);
  });

  it("never touches a hand-written project", async () => {
    // Manual projects have a null repoName, and Postgres treats NULLs as
    // distinct in a unique index, so the two sets cannot collide.
    const manual = await prisma.project.create({
      data: {
        userId,
        name: "Hand written",
        bullets: ["Something I wrote myself"],
        tech: ["C++"],
        source: "manual",
      },
    });
    const github = fakeGitHub([{ name: "Hand written", languages: { Go: 1 } }]);
    await syncGitHubProjects(prisma, userId, { fetchImpl: github.impl });

    const untouched = await prisma.project.findUniqueOrThrow({ where: { id: manual.id } });
    expect(untouched.bullets).toEqual(["Something I wrote myself"]);
    expect(untouched.source).toBe("manual");
    // The repo of the same name arrived as its own row.
    expect(await prisma.project.count({ where: { userId } })).toBe(2);
  });

  it("never writes bullets", async () => {
    // Bullets are the resume's prose -- the user's or the Refactorer's. The sync
    // owns evidence, not anything that appears verbatim on a page.
    const github = fakeGitHub([{ name: "a", readme: "# Title\nSome prose", languages: { Go: 1 } }]);
    await syncGitHubProjects(prisma, userId, { fetchImpl: github.impl });
    const project = await prisma.project.findFirstOrThrow({ where: { userId } });
    expect(project.bullets).toEqual([]);
  });

  it("keeps a repository that disappeared from GitHub", async () => {
    const first = fakeGitHub([{ name: "gone", languages: { Go: 1 } }, { name: "stays", languages: { Go: 1 } }]);
    await syncGitHubProjects(prisma, userId, { fetchImpl: first.impl });

    const second = fakeGitHub([{ name: "stays", languages: { Go: 1 } }]);
    await syncGitHubProjects(prisma, userId, { fetchImpl: second.impl, force: true });

    // Not deleted: the user may have edited its bullets, and deleting is
    // destructive and unasked for.
    const names = (await prisma.project.findMany({ where: { userId } })).map((p) => p.name);
    expect(names.sort()).toEqual(["gone", "stays"]);
  });

  it("dates a long-untouched repository rather than calling it current", async () => {
    const old = new Date(Date.now() - 200 * 86_400_000).toISOString();
    const github = fakeGitHub([{ name: "dormant", pushedAt: old, languages: { Go: 1 } }]);
    await syncGitHubProjects(prisma, userId, { fetchImpl: github.impl });
    const project = await prisma.project.findFirstOrThrow({ where: { userId } });
    // "Present" on a resume for something last touched 200 days ago is a small
    // untruth.
    expect(project.endDate).not.toBeNull();
  });

  it("refuses to sync without a token", async () => {
    await prisma.user.update({ where: { id: userId }, data: { githubToken: null } });
    await expect(syncGitHubProjects(prisma, userId, { fetchImpl: fakeGitHub([]).impl })).rejects.toMatchObject({
      status: 400,
    });
  });

  it("reports an undecryptable stored token instead of sending garbage to GitHub", async () => {
    await prisma.user.update({
      where: { id: userId },
      data: { githubToken: "v1.aaaa.bbbb.cccc" },
    });
    const github = fakeGitHub([]);
    await expect(
      syncGitHubProjects(prisma, userId, { fetchImpl: github.impl }),
    ).rejects.toMatchObject({ status: 400 });
    expect(github.calls).toEqual([]);
  });
});
