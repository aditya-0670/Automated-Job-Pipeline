/**
 * Seed one real profile, so the pipeline has genuine data to work with.
 *
 * The source is `services/ai/tests/fixtures/real_profile.json` -- the same
 * document the AI pipeline is tested against -- rather than a copy living here.
 * A copy would drift, and the moment it did, the database would be seeded with a
 * profile that the pipeline's own tests never exercise. Reaching across services
 * is acceptable for a development seed run from the repository; nothing at
 * runtime does it, and `tests/profile.test.ts` asserts the round trip so the
 * coupling is checked rather than assumed.
 *
 *   npm run db:seed            (needs DATABASE_URL)
 *
 * Idempotent: re-running replaces this user's rows rather than duplicating
 * them, because a seed that can only be run once is a seed nobody runs.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { PrismaClient } from "@prisma/client";

const HERE = dirname(fileURLToPath(import.meta.url));

/**
 * Where the profile and template live.
 *
 * By default, the AI service's fixtures -- the same document the pipeline is
 * tested against, so the two cannot drift. That relative path only exists in a
 * full checkout, which is fine for `make db-seed` and impossible inside the API
 * image, whose build context is `services/api` alone. So the location is
 * overridable: in Kubernetes the fixtures are injected as a ConfigMap and
 * SEED_DATA_DIR points at the mount. One source of truth either way; only the
 * delivery mechanism changes.
 */
const AI_FIXTURES = process.env.SEED_DATA_DIR || resolve(HERE, "../../ai/tests/fixtures");

const prisma = new PrismaClient();

/** The seeded user's email, also used by the dev-mode JWT issuer in Part 13. */
const SEED_EMAIL = process.env.SEED_USER_EMAIL ?? "aditya@resumeforge.dev";

interface Fixture {
  name: string;
  education: Array<Record<string, any>>;
  experiences: Array<Record<string, any>>;
  projects: Array<Record<string, any>>;
  skills: Array<{ name: string; category: string; proficiency?: string }>;
  achievements: Array<{ id: string; title: string; text: string }>;
}

/** "2026-01" -> 2026-01-01 UTC. Midday would be safer against timezone shifts,
 *  but these are `@db.Date` columns: Postgres stores no time at all, and the
 *  reader (`toYearMonth`) is explicitly UTC. */
function monthToDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const [year, month] = value.split("-").map(Number);
  if (!year || !month) throw new Error(`Unparseable month: ${value}`);
  return new Date(Date.UTC(year, month - 1, 1));
}

function readFixture(): { profile: Fixture; latex: string } {
  try {
    return {
      profile: JSON.parse(readFileSync(resolve(AI_FIXTURES, "real_profile.json"), "utf8")),
      latex: readFileSync(resolve(AI_FIXTURES, "real_resume.tex"), "utf8"),
    };
  } catch (cause) {
    throw new Error(
      `Could not read the seed data at ${AI_FIXTURES}. It defaults to the AI ` +
        `service's fixtures, so the seeded profile and the pipeline's tests cannot ` +
        `drift -- run this from a full checkout, or set SEED_DATA_DIR to a ` +
        `directory holding real_profile.json and real_resume.tex (in a cluster, ` +
        `a ConfigMap built from those files).`,
      { cause },
    );
  }
}

async function main(): Promise<void> {
  const { profile, latex } = readFixture();

  const user = await prisma.user.upsert({
    where: { email: SEED_EMAIL },
    update: { name: profile.name, latexTemplate: latex },
    create: { email: SEED_EMAIL, name: profile.name, latexTemplate: latex },
  });

  // Replace rather than merge. An upsert per row would leave behind anything
  // deleted from the fixture, so the database would slowly accumulate evidence
  // the pipeline is no longer tested with -- and stale evidence is exactly what
  // the factual guardrail exists to catch.
  await prisma.$transaction([
    prisma.workExperience.deleteMany({ where: { userId: user.id } }),
    prisma.project.deleteMany({ where: { userId: user.id } }),
    prisma.skill.deleteMany({ where: { userId: user.id } }),
    prisma.education.deleteMany({ where: { userId: user.id } }),
    prisma.achievement.deleteMany({ where: { userId: user.id } }),
  ]);

  await prisma.$transaction([
    prisma.workExperience.createMany({
      data: profile.experiences.map((exp) => ({
        userId: user.id,
        company: exp.company,
        role: exp.role,
        location: exp.location ?? null,
        startDate: monthToDate(exp.start)!,
        endDate: monthToDate(exp.end),
        bullets: exp.bullets ?? [],
        detail: exp.detail ?? null,
      })),
    }),
    prisma.project.createMany({
      data: profile.projects.map((proj) => ({
        userId: user.id,
        name: proj.name,
        tech: proj.tech ?? [],
        bullets: proj.bullets ?? [],
        detail: proj.detail ?? null,
        startDate: monthToDate(proj.start),
        endDate: monthToDate(proj.end),
        source: "manual",
      })),
    }),
    prisma.skill.createMany({
      data: profile.skills.map((skill) => ({
        userId: user.id,
        name: skill.name,
        category: skill.category,
        proficiency: skill.proficiency ?? null,
      })),
    }),
    prisma.education.createMany({
      data: profile.education.map((edu) => ({
        userId: user.id,
        institution: edu.institution,
        degree: edu.degree,
        field: edu.field ?? null,
        gpa: edu.gpa ?? null,
        startYear: edu.start_year ?? null,
        endYear: edu.end_year ?? null,
        courses: edu.courses ?? [],
      })),
    }),
    prisma.achievement.createMany({
      data: profile.achievements.map((ach) => ({
        userId: user.id,
        title: ach.title,
        detail: ach.text,
      })),
    }),
  ]);

  const counts = {
    experiences: profile.experiences.length,
    projects: profile.projects.length,
    skills: profile.skills.length,
    education: profile.education.length,
    achievements: profile.achievements.length,
    latexBytes: latex.length,
  };
  console.log(`Seeded ${user.email} (${user.id}):`, counts);
}

main()
  .catch((error) => {
    console.error(error);
    // Non-zero, so `make db-seed` and CI fail loudly rather than reporting a
    // successful seed of nothing.
    process.exitCode = 1;
  })
  .finally(() => prisma.$disconnect());
