/**
 * GitHub repository sync.
 *
 * The product claim this serves is "your projects, without retyping them", and
 * the constraint is GitHub's rate limit: 5,000 requests an hour with a PAT, and
 * a naive sync of 40 repositories costs 120 of them (list + languages + README).
 * So two mechanisms, and they are different things:
 *
 *   * **The TTL** answers "should we sync at all?" without any network call.
 *   * **The ETag** answers "did anything change?" with a request GitHub does not
 *     charge to the rate limit — a 304 is free.
 *
 * `fetchImpl` is injectable so the tests drive real HTTP semantics (304s, 401s,
 * pagination) against a fake rather than mocking this module's own functions,
 * which would test nothing.
 */

import { logger } from "../logger.js";
import { ApiError } from "../middleware/errors.js";

const API = "https://api.github.com";
const USER_AGENT = "resumeforge-api";
/** Repos per page. 100 is GitHub's maximum, so this is the fewest requests. */
const PAGE_SIZE = 100;
/** A stop on pagination. Someone with 1,000 repositories is not the user this
 *  feature is for, and an unbounded loop against a paginated API is how a sync
 *  job runs for an hour. */
const MAX_PAGES = 5;

export interface GitHubRepo {
  name: string;
  full_name: string;
  description: string | null;
  html_url: string;
  fork: boolean;
  archived: boolean;
  private: boolean;
  stargazers_count: number;
  topics?: string[];
  language: string | null;
  pushed_at: string;
  created_at: string;
}

export interface SyncedProject {
  repoName: string;
  repoUrl: string;
  description: string | null;
  stars: number;
  topics: string[];
  languages: Record<string, number>;
  readme: string | null;
  pushedAt: Date;
  createdAt: Date;
}

export interface RepoListResult {
  /** Null when GitHub answered 304: nothing has changed since `etag`. */
  repos: GitHubRepo[] | null;
  etag: string | null;
  /** Requests actually charged against the rate limit. Asserted in tests. */
  requests: number;
  rateLimitRemaining: number | null;
}

type FetchImpl = typeof fetch;

export class GitHubClient {
  private requests = 0;

  constructor(
    private readonly token: string,
    private readonly fetchImpl: FetchImpl = fetch,
  ) {}

  get requestCount(): number {
    return this.requests;
  }

  private async get(path: string, headers: Record<string, string> = {}): Promise<Response> {
    this.requests += 1;
    let response: Response;
    try {
      response = await this.fetchImpl(`${API}${path}`, {
        headers: {
          authorization: `Bearer ${this.token}`,
          accept: "application/vnd.github+json",
          // Pinned: GitHub's default version moves, and `topics` was behind a
          // preview header not long ago. An unpinned client breaks silently.
          "x-github-api-version": "2022-11-28",
          "user-agent": USER_AGENT,
          ...headers,
        },
        signal: AbortSignal.timeout(15_000),
      });
    } catch (err) {
      logger.error({ err, path }, "GitHub request failed");
      throw ApiError.upstream("GitHub is not reachable right now.", 502);
    }

    if (response.status === 401) {
      // The user's problem, not ours, and specific: a revoked or mistyped PAT.
      throw new ApiError(400, "github_unauthorized", "Your GitHub token was rejected. Re-add it.");
    }
    if (response.status === 403 || response.status === 429) {
      const remaining = response.headers.get("x-ratelimit-remaining");
      if (remaining === "0") {
        const reset = response.headers.get("x-ratelimit-reset");
        throw new ApiError(429, "github_rate_limited", "GitHub's rate limit is exhausted.", {
          resetAt: reset ? new Date(Number(reset) * 1000).toISOString() : undefined,
        });
      }
      throw new ApiError(403, "github_forbidden", "GitHub refused the request.");
    }
    return response;
  }

  /** The user's repositories, or null if the ETag says nothing changed. */
  async listRepos(etag: string | null): Promise<RepoListResult> {
    const repos: GitHubRepo[] = [];
    let latestEtag: string | null = null;
    let rateLimitRemaining: number | null = null;

    for (let page = 1; page <= MAX_PAGES; page += 1) {
      // The conditional request only makes sense for page 1: it is the page
      // whose ETag was stored, and if it is unchanged the rest are too --
      // `sort=pushed` means any change anywhere reorders page 1.
      const conditional: Record<string, string> =
        page === 1 && etag ? { "if-none-match": etag } : {};
      const response = await this.get(
        `/user/repos?per_page=${PAGE_SIZE}&page=${page}&sort=pushed&affiliation=owner`,
        conditional,
      );
      rateLimitRemaining =
        Number(response.headers.get("x-ratelimit-remaining")) || rateLimitRemaining;

      if (response.status === 304) {
        return { repos: null, etag, requests: this.requests, rateLimitRemaining };
      }
      if (!response.ok) {
        throw ApiError.upstream(`GitHub returned ${response.status} listing repositories.`);
      }
      if (page === 1) latestEtag = response.headers.get("etag");

      const batch = (await response.json()) as GitHubRepo[];
      repos.push(...batch);
      if (batch.length < PAGE_SIZE) break;
    }

    return { repos, etag: latestEtag, requests: this.requests, rateLimitRemaining };
  }

  private async languages(repo: GitHubRepo): Promise<Record<string, number>> {
    const response = await this.get(`/repos/${repo.full_name}/languages`);
    if (!response.ok) return {};
    return (await response.json()) as Record<string, number>;
  }

  private async readme(repo: GitHubRepo): Promise<string | null> {
    // Raw, not the JSON envelope: the envelope is base64 and this is only ever
    // read as text for keyword matching.
    const response = await this.get(`/repos/${repo.full_name}/readme`, {
      accept: "application/vnd.github.raw+json",
    });
    // 404 is the normal answer for a repo with no README, not a failure.
    if (response.status === 404 || !response.ok) return null;
    const text = await response.text();
    // Truncated because this ends up in an LLM prompt via the evidence index; a
    // 200KB README would eat the whole token budget on one project.
    return text.length > 20_000 ? `${text.slice(0, 20_000)}\n[truncated]` : text;
  }

  /**
   * Enrich the repositories worth keeping.
   *
   * Forks and archived repositories are dropped, and dropped *before* the
   * per-repo requests rather than after: a fork is not evidence of the user's
   * work, and paying two API calls to find that out is the expensive way to
   * learn it. Private repos are kept — a resume can legitimately cite work the
   * reader cannot open — but their README is not, since the point of enrichment
   * is public evidence and a private README is the likeliest place for something
   * the user did not mean to publish.
   */
  async enrich(repos: GitHubRepo[]): Promise<SyncedProject[]> {
    const kept = repos.filter((r) => !r.fork && !r.archived);
    logger.info(
      { total: repos.length, kept: kept.length },
      "GitHub repos after dropping forks and archived",
    );

    const projects: SyncedProject[] = [];
    for (const repo of kept) {
      const [languages, readme] = await Promise.all([
        this.languages(repo),
        repo.private ? Promise.resolve(null) : this.readme(repo),
      ]);
      projects.push({
        repoName: repo.name,
        repoUrl: repo.html_url,
        description: repo.description,
        stars: repo.stargazers_count,
        topics: repo.topics ?? [],
        languages,
        readme,
        pushedAt: new Date(repo.pushed_at),
        createdAt: new Date(repo.created_at),
      });
    }
    return projects;
  }
}

/** Whether a sync is still inside its freshness window. */
export function isFresh(syncedAt: Date | null, ttlMinutes: number, now = new Date()): boolean {
  if (!syncedAt) return false;
  return now.getTime() - syncedAt.getTime() < ttlMinutes * 60_000;
}

/**
 * The text a synced repository contributes as evidence.
 *
 * Assembled here rather than in the AI service because the AI service indexes
 * `detail` as opaque text; what goes into it is a product decision about what
 * counts as evidence of a project.
 */
export function projectDetail(project: SyncedProject): string {
  const parts = [
    project.description,
    Object.keys(project.languages).length > 0
      ? `Languages: ${Object.keys(project.languages).join(", ")}.`
      : null,
    project.topics.length > 0 ? `Topics: ${project.topics.join(", ")}.` : null,
    project.readme,
  ];
  return parts.filter(Boolean).join("\n\n");
}

/** Languages, most bytes first — the order a reader expects on a resume. */
export function techFromLanguages(languages: Record<string, number>): string[] {
  return Object.entries(languages)
    .sort(([, a], [, b]) => b - a)
    .map(([name]) => name);
}
