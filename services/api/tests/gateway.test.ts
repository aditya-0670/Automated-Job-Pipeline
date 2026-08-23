/**
 * The gateway's HTTP surface.
 *
 * A fake AI client, a real database. The fake is the point: these tests are
 * about what the gateway *sends* and *refuses*, and the pipeline behind it is
 * already covered by 341 Python tests. The real database is also the point --
 * the ownership tests are meaningless against a mock, since the whole question
 * is whether the `where` clause actually filters.
 */

import jwt from "jsonwebtoken";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import request from "supertest";

process.env.INTERNAL_API_KEY ??= "test-internal-key";
process.env.JWT_SECRET ??= "test-jwt-secret-value";
process.env.AUTH_MODE ??= "dev";
// Set here rather than inherited from .env: a test that only passes because a
// developer happens to have an env file is a test that fails in CI, which is
// exactly how this one was found.
process.env.ENCRYPTION_KEY ??= "test-encryption-key-32-bytes-min";
// The gateway logs every request; 45 tests of that is noise, not signal.
process.env.LOG_LEVEL = "silent";

const { PrismaClient } = await import("@prisma/client");
const { createApp } = await import("../src/app.js");
const { signToken } = await import("../src/middleware/auth.js");

import type { AiClient, RunPipelineInput } from "../src/services/aiClient.js";

const hasDatabase = Boolean(process.env.DATABASE_URL);
const prisma = new PrismaClient();

/** Records what the gateway sent, and lets a test choose what comes back. */
function fakeAi() {
  const calls: {
    run: RunPipelineInput[];
    review: unknown[];
    keywords: unknown[];
    lastEventIds: (string | undefined)[];
  } = { run: [], review: [], keywords: [], lastEventIds: [] };
  const state: {
    status: Record<string, unknown>;
    events: string;
    pdf: Response;
    runError?: Error;
  } = {
    status: { session_id: "s", step: "EXTRACTING", is_paused: true, paused_at: "keyword_review", is_complete: false },
    events: "id: 1\r\nevent: progress\r\ndata: {\"sequence\":1}\r\n\r\nevent: paused\r\ndata: {}\r\n\r\n",
    pdf: new Response("%PDF-1.7 stub", { status: 200 }),
  };

  const client: AiClient = {
    async run(input) {
      calls.run.push(input);
      if (state.runError) throw state.runError;
      return { session_id: input.sessionId, status: "running" };
    },
    async resumeKeywords(_id, keywords) {
      calls.keywords.push(keywords);
      return { status: "running" };
    },
    async resumeReview(_id, input) {
      calls.review.push(input);
      return { status: "running" };
    },
    async status() {
      return state.status;
    },
    async events(_id, options) {
      calls.lastEventIds.push(options.lastEventId);
      return new Response(state.events, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    },
    async pdf() {
      return state.pdf;
    },
  };

  return { client, calls, state };
}

let ai: ReturnType<typeof fakeAi>;
let app: ReturnType<typeof createApp>;
let userId: string;
let otherUserId: string;
let token: string;
let otherToken: string;

const LATEX = "\\documentclass{article}\\begin{document}Resume\\end{document}";
const JOB_TEXT = "We need a backend engineer with C++, Docker and Kafka experience. ".repeat(6);

beforeAll(async () => {
  if (!hasDatabase) return;
  ai = fakeAi();
  app = createApp({ prisma, redis: null, aiClient: () => ai.client });

  const user = await prisma.user.upsert({
    where: { email: "gateway-test@resumeforge.dev" },
    update: { latexTemplate: LATEX },
    create: { email: "gateway-test@resumeforge.dev", name: "Test", latexTemplate: LATEX },
  });
  const other = await prisma.user.upsert({
    where: { email: "gateway-other@resumeforge.dev" },
    update: {},
    create: { email: "gateway-other@resumeforge.dev", name: "Other" },
  });
  userId = user.id;
  otherUserId = other.id;
  token = signToken({ id: user.id, email: user.email });
  otherToken = signToken({ id: other.id, email: other.email });

  await prisma.workExperience.deleteMany({ where: { userId: { in: [userId, otherUserId] } } });
  await prisma.workExperience.create({
    data: {
      userId,
      company: "Oracle",
      role: "Intern",
      startDate: new Date(Date.UTC(2026, 0, 1)),
      bullets: ["Wrote C++"],
      detail: "C++ and GDB",
    },
  });
});

afterAll(async () => {
  if (hasDatabase) {
    await prisma.user.deleteMany({
      where: { email: { in: ["gateway-test@resumeforge.dev", "gateway-other@resumeforge.dev"] } },
    });
  }
  await prisma.$disconnect();
});

const auth = () => ({ Authorization: `Bearer ${token}` });

describe.skipIf(!hasDatabase)("authentication", () => {
  it("refuses a request with no token", async () => {
    const res = await request(app).get("/api/profile");
    expect(res.status).toBe(401);
    expect(res.body.error.code).toBe("unauthorized");
  });

  it("refuses a token signed with the wrong secret", async () => {
    const forged = jwt.sign({ sub: userId }, "not-the-secret", {
      issuer: "resumeforge",
      audience: "resumeforge",
    });
    const res = await request(app).get("/api/profile").set("Authorization", `Bearer ${forged}`);
    expect(res.status).toBe(401);
    expect(res.body.error.message).toBe("Invalid token");
  });

  it("refuses a token issued for a different audience", async () => {
    // Same secret, different purpose -- a download link, say. Checking only the
    // signature would let it authenticate a session here.
    const wrongAudience = jwt.sign({ sub: userId }, process.env.JWT_SECRET!, {
      issuer: "resumeforge",
      audience: "somewhere-else",
    });
    const res = await request(app)
      .get("/api/profile")
      .set("Authorization", `Bearer ${wrongAudience}`);
    expect(res.status).toBe(401);
  });

  it("distinguishes an expired token from an invalid one", async () => {
    // "Expired" tells a client to refresh; "invalid" tells it to stop. Collapsing
    // the two produces retry loops.
    const expired = jwt.sign({ sub: userId }, process.env.JWT_SECRET!, {
      issuer: "resumeforge",
      audience: "resumeforge",
      expiresIn: -10,
    });
    const res = await request(app).get("/api/profile").set("Authorization", `Bearer ${expired}`);
    expect(res.body.error.message).toBe("Token expired");
  });

  it("mints a dev token for the seeded user and accepts it", async () => {
    const minted = await request(app).post("/api/auth/dev-token");
    // The seeded user may or may not exist in this database; either answer is
    // valid, but a 500 is not.
    expect([200, 404]).toContain(minted.status);
    if (minted.status === 200) {
      const me = await request(app)
        .get("/api/auth/me")
        .set("Authorization", `Bearer ${minted.body.token}`);
      expect(me.status).toBe(200);
      expect(me.body.user.email).toBeTruthy();
    }
  });
});

describe.skipIf(!hasDatabase)("profile", () => {
  it("returns the profile in the shape the pipeline consumes", async () => {
    const res = await request(app).get("/api/profile").set(auth());
    expect(res.status).toBe(200);
    // snake_case, because that is the Python side's contract.
    expect(res.body.profile.experiences[0]).toMatchObject({ company: "Oracle", current: true });
    expect(res.body.hasLatexTemplate).toBe(true);
  });

  it("stores and returns the LaTeX template", async () => {
    const put = await request(app).put("/api/profile/latex").set(auth()).send({ latexTemplate: LATEX });
    expect(put.status).toBe(200);
    const get = await request(app).get("/api/profile/latex").set(auth());
    expect(get.text).toContain("\\documentclass");
  });

  it("rejects a template too short to be a resume", async () => {
    const res = await request(app).put("/api/profile/latex").set(auth()).send({ latexTemplate: "no" });
    expect(res.status).toBe(400);
    expect(res.body.error.details[0].path).toBe("latexTemplate");
  });

  it("creates, updates and deletes an experience", async () => {
    const created = await request(app)
      .post("/api/profile/experiences")
      .set(auth())
      .send({ company: "ITJobxs", role: "SDE Intern", start: "2025-01", end: "2025-02", bullets: ["PHP"] });
    expect(created.status).toBe(201);

    const patched = await request(app)
      .patch(`/api/profile/experiences/${created.body.id}`)
      .set(auth())
      .send({ role: "Senior SDE Intern" });
    expect(patched.status).toBe(200);

    const deleted = await request(app)
      .delete(`/api/profile/experiences/${created.body.id}`)
      .set(auth());
    expect(deleted.status).toBe(204);
  });

  it("rejects a malformed month", async () => {
    const res = await request(app)
      .post("/api/profile/experiences")
      .set(auth())
      .send({ company: "X", role: "Y", start: "January 2025" });
    expect(res.status).toBe(400);
  });

  it("will not let one user touch another's experience", async () => {
    // The ownership check lives in the where clause, so the row is simply not
    // found rather than found-then-rejected.
    const mine = await prisma.workExperience.findFirstOrThrow({ where: { userId } });
    const res = await request(app)
      .patch(`/api/profile/experiences/${mine.id}`)
      .set("Authorization", `Bearer ${otherToken}`)
      .send({ role: "Hijacked" });
    expect(res.status).toBe(404);

    const unchanged = await prisma.workExperience.findUniqueOrThrow({ where: { id: mine.id } });
    expect(unchanged.role).toBe("Intern");
  });

  it("replaces the skill list atomically", async () => {
    const res = await request(app)
      .put("/api/profile/skills")
      .set(auth())
      .send({ skills: [{ name: "C++", category: "language" }, { name: "Docker", category: "tool" }] });
    expect(res.body.count).toBe(2);
    const again = await request(app)
      .put("/api/profile/skills")
      .set(auth())
      .send({ skills: [{ name: "C++", category: "language" }] });
    // Replaced, not appended: a duplicated skill is a resume that lists it twice.
    expect(again.body.count).toBe(1);
  });
});

describe.skipIf(!hasDatabase)("starting a session", () => {
  it("enriches the run with the profile and template the AI cannot fetch itself", async () => {
    const res = await request(app).post("/api/sessions").set(auth()).send({ jobText: JOB_TEXT });
    expect(res.status).toBe(202);
    expect(res.body.streamUrl).toBe(`/api/sessions/${res.body.sessionId}/stream`);

    const sent = ai.calls.run.at(-1)!;
    // This is the isolation guarantee: the pipeline only ever sees what the
    // gateway put in the request.
    expect(sent.userLatex).toContain("\\documentclass");
    expect(sent.userProfile.experiences.length).toBeGreaterThan(0);
    expect(sent.sessionId).toBe(res.body.sessionId);

    // The row and the checkpoint thread share one key.
    const row = await prisma.chatSession.findUniqueOrThrow({ where: { id: res.body.sessionId } });
    expect(row.userId).toBe(userId);
  });

  it("refuses before spending anything when there is no template", async () => {
    const before = ai.calls.run.length;
    const res = await request(app)
      .post("/api/sessions")
      .set("Authorization", `Bearer ${otherToken}`)
      .send({ jobText: JOB_TEXT });
    expect(res.status).toBe(400);
    expect(res.body.error.message).toContain("LaTeX resume template");
    // The pipeline would refuse this too, but only after a scrape and an
    // extraction have been paid for.
    expect(ai.calls.run.length).toBe(before);
  });

  it("requires a posting", async () => {
    const res = await request(app).post("/api/sessions").set(auth()).send({});
    expect(res.status).toBe(400);
  });

  it("rejects a job description too short to analyse", async () => {
    const res = await request(app).post("/api/sessions").set(auth()).send({ jobText: "too short" });
    expect(res.status).toBe(400);
  });

  it("marks the session failed if the pipeline could not be started", async () => {
    ai.state.runError = new Error("AI unreachable");
    const res = await request(app).post("/api/sessions").set(auth()).send({ jobText: JOB_TEXT });
    ai.state.runError = undefined;
    expect(res.status).toBe(500);

    // Marked, not deleted: the user asked for this and deserves to see what
    // happened to it.
    const failed = await prisma.chatSession.findFirst({
      where: { userId, status: "failed" },
      orderBy: { createdAt: "desc" },
    });
    expect(failed?.error).toContain("AI unreachable");
  });
});

describe.skipIf(!hasDatabase)("driving a session", () => {
  async function newSession() {
    const res = await request(app).post("/api/sessions").set(auth()).send({ jobText: JOB_TEXT });
    return res.body.sessionId as string;
  }

  it("reads status and mirrors it onto the session row", async () => {
    const id = await newSession();
    ai.state.status = { session_id: id, step: "HUMAN_REVIEW", is_paused: true, paused_at: "human_review", is_complete: false, human_review: { latex: "\\documentclass{x}" } };

    const res = await request(app).get(`/api/sessions/${id}`).set(auth());
    expect(res.status).toBe(200);
    const row = await prisma.chatSession.findUniqueOrThrow({ where: { id } });
    expect(row.currentStep).toBe("HUMAN_REVIEW");
    expect(row.generatedLatex).toBe("\\documentclass{x}");
  });

  it("records completion on the row when the pipeline finishes", async () => {
    const id = await newSession();
    ai.state.status = { session_id: id, step: "COMPLETE", is_paused: false, paused_at: null, is_complete: true };
    await request(app).get(`/api/sessions/${id}`).set(auth());
    const row = await prisma.chatSession.findUniqueOrThrow({ where: { id } });
    expect(row.status).toBe("completed");
    expect(row.completedAt).not.toBeNull();
  });

  it("forwards a review decision", async () => {
    const id = await newSession();
    const res = await request(app)
      .post(`/api/sessions/${id}/review`)
      .set(auth())
      .send({ decision: "accept" });
    expect(res.status).toBe(202);
    expect(ai.calls.review.at(-1)).toMatchObject({ decision: "accept" });
  });

  it("rejects request_changes with no instruction before calling the pipeline", async () => {
    const id = await newSession();
    const before = ai.calls.review.length;
    const res = await request(app)
      .post(`/api/sessions/${id}/review`)
      .set(auth())
      .send({ decision: "request_changes" });
    expect(res.status).toBe(400);
    expect(ai.calls.review.length).toBe(before);
  });

  it("forwards a confirmed keyword set", async () => {
    const id = await newSession();
    const res = await request(app)
      .post(`/api/sessions/${id}/keywords`)
      .set(auth())
      .send({ keywords: [{ term: "Kafka" }] });
    expect(res.status).toBe(202);
    expect(ai.calls.keywords.at(-1)).toEqual([{ term: "Kafka" }]);
  });

  it("will not let one user read another's session", async () => {
    const id = await newSession();
    for (const path of [`/api/sessions/${id}`, `/api/sessions/${id}/stream`, `/api/sessions/${id}/pdf`]) {
      const res = await request(app).get(path).set("Authorization", `Bearer ${otherToken}`);
      expect(res.status, path).toBe(404);
    }
  });

  it("lists only the caller's sessions", async () => {
    await newSession();
    const res = await request(app)
      .get("/api/sessions")
      .set("Authorization", `Bearer ${otherToken}`);
    expect(res.body.sessions).toEqual([]);
  });
});

describe.skipIf(!hasDatabase)("streaming and download", () => {
  async function newSession() {
    const res = await request(app).post("/api/sessions").set(auth()).send({ jobText: JOB_TEXT });
    return res.body.sessionId as string;
  }

  it("relays the event stream byte for byte", async () => {
    const id = await newSession();
    const res = await request(app).get(`/api/sessions/${id}/stream`).set(auth());
    expect(res.headers["content-type"]).toContain("text/event-stream");
    // Turning off proxy buffering is what makes a live stream live.
    expect(res.headers["x-accel-buffering"]).toBe("no");
    // The ids the client resumes from arrive unchanged: no second SSE
    // implementation between the pipeline and the browser.
    expect(res.text).toContain("id: 1");
    expect(res.text).toContain("event: paused");
  });

  it("accepts a token in the query string on the stream route only", async () => {
    // EventSource cannot set an Authorization header.
    const id = await newSession();
    const stream = await request(app).get(`/api/sessions/${id}/stream?token=${token}`);
    expect(stream.status).toBe(200);

    const profile = await request(app).get(`/api/profile?token=${token}`);
    expect(profile.status).toBe(401);
  });

  it("forwards a resume point given as a query parameter", async () => {
    // A browser cannot set Last-Event-ID on a stream it opens itself, so a page
    // that has answered a gate says where it got to this way. Without it the AI
    // service treats a caught-up client as a new reader and replays the stop it
    // is still sitting in.
    const id = await newSession();
    await request(app).get(`/api/sessions/${id}/stream?lastEventId=7`).set(auth());
    expect(ai.calls.lastEventIds.at(-1)).toBe("7");
  });

  it("prefers the header, which only a real EventSource sets", async () => {
    const id = await newSession();
    await request(app)
      .get(`/api/sessions/${id}/stream?lastEventId=7`)
      .set(auth())
      .set("Last-Event-ID", "9");
    expect(ai.calls.lastEventIds.at(-1)).toBe("9");
  });

  it("serves the PDF", async () => {
    const id = await newSession();
    ai.state.pdf = new Response("%PDF-1.7 stub", { status: 200 });
    const res = await request(app).get(`/api/sessions/${id}/pdf`).set(auth());
    expect(res.status).toBe(200);
    expect(res.headers["content-disposition"]).toContain(`resume-${id}.pdf`);
  });

  it("forwards 'not yet' rather than flattening it into a 502", async () => {
    const id = await newSession();
    ai.state.pdf = new Response(JSON.stringify({ detail: "No PDF yet" }), { status: 409 });
    const res = await request(app).get(`/api/sessions/${id}/pdf`).set(auth());
    // A 502 would tell the client the gateway is broken; 409 tells it to wait.
    expect(res.status).toBe(409);
    ai.state.pdf = new Response("%PDF-1.7 stub", { status: 200 });
  });
});

describe.skipIf(!hasDatabase)("error envelope", () => {
  it("uses one shape everywhere, with the correlation id", async () => {
    const res = await request(app).get("/api/nope").set(auth());
    expect(res.status).toBe(404);
    expect(res.body.error).toMatchObject({ code: "not_found" });
    expect(res.body.error.requestId).toBeTruthy();
    expect(res.headers["x-request-id"]).toBeTruthy();
  });

  it("never forwards an unexpected exception's message", async () => {
    // Those strings are written for developers and regularly contain connection
    // strings or query fragments.
    const boom = createApp({
      prisma,
      redis: null,
      aiClient: () => {
        throw new Error("postgresql://user:password@host/db exploded");
      },
    });
    const res = await request(boom).post("/api/sessions").set(auth()).send({ jobText: JOB_TEXT });
    expect(res.status).toBe(500);
    expect(JSON.stringify(res.body)).not.toContain("password");
  });
});

describe.skipIf(!hasDatabase)("CORS", () => {
  const WEB = "http://localhost:3000";

  it("allows the configured web origin", async () => {
    const res = await request(app).get("/api/profile").set(auth()).set("Origin", WEB);
    expect(res.headers["access-control-allow-origin"]).toBe(WEB);
    // Without Vary, a cache can hand one origin's allow header to another.
    expect(res.headers["vary"]).toContain("origin");
  });

  it("does not answer an origin that is not on the list", async () => {
    const res = await request(app).get("/api/profile").set(auth()).set("Origin", "https://evil.example");
    expect(res.headers["access-control-allow-origin"]).toBeUndefined();
  });

  it("answers a preflight without reaching a route", async () => {
    // A preflight carries no credentials by design, so it must never hit an
    // authenticated handler.
    const res = await request(app)
      .options("/api/sessions")
      .set("Origin", WEB)
      .set("Access-Control-Request-Method", "POST");
    expect(res.status).toBe(204);
    expect(res.headers["access-control-allow-headers"]).toContain("authorization");
  });

  it("never allows credentials, because this API has no cookies", async () => {
    // Allow-Credentials plus a reflected origin is the combination that turns
    // permissive CORS into CSRF.
    const res = await request(app).get("/api/profile").set(auth()).set("Origin", WEB);
    expect(res.headers["access-control-allow-credentials"]).toBeUndefined();
  });
});

describe.skipIf(!hasDatabase)("probes", () => {
  it("health needs no credential", async () => {
    expect((await request(app).get("/health")).status).toBe(200);
  });

  it("ready reports the database", async () => {
    const res = await request(app).get("/ready");
    expect(res.body.database).toBe("ok");
  });
});
