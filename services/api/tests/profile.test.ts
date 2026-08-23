/**
 * The schema's real acceptance test: can these tables reproduce the document the
 * pipeline is tested against?
 *
 * A schema that stores plausible-looking user data but cannot rebuild
 * `real_profile.json` is a schema that fails in Part 13, after the gateway is
 * written against it. Asserting the round trip here is what turned up the
 * missing `Achievement` model -- the AI's evidence index reads achievements, and
 * without that table the seeded profile was quietly missing five pieces of
 * evidence.
 *
 * The database half skips without DATABASE_URL, so `npm test` stays hermetic.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { PROFILE_INCLUDE, toAiProfile, toYearMonth, type ProfileRows } from "../src/profile.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = JSON.parse(
  readFileSync(resolve(HERE, "../../ai/tests/fixtures/real_profile.json"), "utf8"),
);

const hasDatabase = Boolean(process.env.DATABASE_URL);
/** The seeded user, addressed by email rather than as "the first row". Other
 *  suites create their own users, and `findFirst` with no ordering would pick
 *  whichever one Postgres felt like returning. */
const SEED_EMAIL = process.env.SEED_USER_EMAIL ?? "aditya@resumeforge.dev";

/** Ids differ by construction -- the fixture's are hand-written, the database's
 *  are cuids -- so they are dropped before comparison. Everything else must
 *  match exactly. */
function withoutIds<T extends { id: string }>(rows: T[]): Omit<T, "id">[] {
  return rows.map(({ id: _id, ...rest }) => rest as Omit<T, "id">);
}

const byName = (a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name);

describe("toYearMonth", () => {
  it("formats a date column as the AI service writes months", () => {
    expect(toYearMonth(new Date(Date.UTC(2026, 0, 1)))).toBe("2026-01");
    expect(toYearMonth(new Date(Date.UTC(2025, 11, 1)))).toBe("2025-12");
  });

  it("returns null for an ongoing role", () => {
    expect(toYearMonth(null)).toBeNull();
  });

  it("reads in UTC, so a date-only column cannot slip a month", () => {
    // 2026-01-01 read in a timezone behind UTC would otherwise be December 2025:
    // an off-by-one-month that appears only for users west of Greenwich.
    const midnightUtc = new Date("2026-01-01T00:00:00.000Z");
    expect(toYearMonth(midnightUtc)).toBe("2026-01");
  });
});

describe("toAiProfile", () => {
  const rows: ProfileRows = {
    id: "user-1",
    name: "Test User",
    experiences: [
      {
        id: "e1",
        company: "Oracle",
        role: "Intern",
        location: "Hyderabad",
        startDate: new Date(Date.UTC(2026, 0, 1)),
        endDate: null,
        bullets: ["Did a thing"],
        detail: "Context",
      },
    ],
    projects: [],
    achievements: [],
    education: [],
    skills: [],
  };

  it("derives `current` from the absence of an end date", () => {
    // One fact, one column: an `isCurrent` boolean alongside `endDate` is two
    // columns that can disagree.
    const profile = toAiProfile(rows);
    expect(profile.experiences[0]).toMatchObject({ current: true, end: null, start: "2026-01" });
  });

  it("marks a finished role as not current", () => {
    const finished = {
      ...rows,
      experiences: [{ ...rows.experiences[0]!, endDate: new Date(Date.UTC(2025, 1, 1)) }],
    };
    expect(toAiProfile(finished).experiences[0]).toMatchObject({ current: false, end: "2025-02" });
  });

  it("substitutes empty strings for absent free text, never null", () => {
    // The Python side concatenates these into the text it indexes; a null would
    // become the literal string "None" in the evidence corpus.
    const sparse = { ...rows, experiences: [{ ...rows.experiences[0]!, detail: null }] };
    expect(toAiProfile(sparse).experiences[0]!.detail).toBe("");
  });

  it("names every relation the pipeline needs in PROFILE_INCLUDE", () => {
    // A route that forgets one ships a profile with no projects, which the
    // pipeline reports as "no relevant experience found" rather than as a bug.
    expect(Object.keys(PROFILE_INCLUDE).sort()).toEqual([
      "achievements",
      "education",
      "experiences",
      "projects",
      "skills",
    ]);
  });
});

describe.skipIf(!hasDatabase)("the seeded database reproduces the pipeline's fixture", () => {
  async function seededProfile() {
    const { PrismaClient } = await import("@prisma/client");
    const prisma = new PrismaClient();
    try {
      const user = await prisma.user.findUniqueOrThrow({
        where: { email: SEED_EMAIL },
        include: PROFILE_INCLUDE as never,
      });
      return toAiProfile(user as unknown as ProfileRows);
    } finally {
      await prisma.$disconnect();
    }
  }

  it("reproduces the experiences exactly", async () => {
    const profile = await seededProfile();
    expect(withoutIds(profile.experiences)).toEqual(withoutIds(FIXTURE.experiences));
  });

  it("reproduces the projects exactly", async () => {
    const profile = await seededProfile();
    expect(withoutIds(profile.projects)).toEqual(withoutIds(FIXTURE.projects));
  });

  it("reproduces the education entry, rendered summary included", async () => {
    const profile = await seededProfile();
    expect(withoutIds(profile.education)).toEqual(withoutIds(FIXTURE.education));
  });

  it("reproduces every achievement", async () => {
    const profile = await seededProfile();
    expect(withoutIds(profile.achievements)).toEqual(withoutIds(FIXTURE.achievements));
  });

  it("reproduces every skill", async () => {
    const profile = await seededProfile();
    // Order differs (the query sorts by name, the fixture is grouped by hand)
    // and does not matter: the AI side indexes skills as a set.
    expect([...profile.skills].sort(byName)).toEqual([...FIXTURE.skills].sort(byName));
  });

  it("stores the LaTeX template the Refactorer must preserve", async () => {
    const { PrismaClient } = await import("@prisma/client");
    const prisma = new PrismaClient();
    try {
      const user = await prisma.user.findUniqueOrThrow({ where: { email: SEED_EMAIL } });
      expect(user.latexTemplate).toContain("\\documentclass");
      expect(user.latexTemplate).toContain("\\end{document}");
    } finally {
      await prisma.$disconnect();
    }
  });
});
