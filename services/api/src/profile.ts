/**
 * Turning database rows into the document the AI service consumes.
 *
 * The AI service is deliberately ignorant of user storage: it holds no database
 * credentials and fetches nothing, so every pipeline run is handed a
 * `user_profile` document by the gateway. That document's shape is fixed by
 * `services/ai/app/matching/profile_index.py`, which indexes five kinds of
 * evidence -- experiences, projects, achievements, education and the skills
 * list -- and by the fixture the pipeline is tested against
 * (`services/ai/tests/fixtures/real_profile.json`).
 *
 * This module is the only place that knows both shapes. It is snake_case on the
 * way out because that is the Python side's contract, not because the database
 * is inconsistent.
 */

/** One month, as the AI side writes it: "2026-01", or null for ongoing. */
export type YearMonth = string | null;

export interface AiExperience {
  id: string;
  company: string;
  role: string;
  location: string | null;
  start: YearMonth;
  end: YearMonth;
  current: boolean;
  bullets: string[];
  detail: string;
}

export interface AiProject {
  id: string;
  name: string;
  tech: string[];
  start: YearMonth;
  end?: YearMonth;
  current: boolean;
  bullets: string[];
  detail: string;
}

export interface AiAchievement {
  id: string;
  title: string;
  text: string;
}

export interface AiEducation {
  id: string;
  institution: string;
  degree: string;
  field: string | null;
  gpa: number | null;
  start_year: number | null;
  end_year: number | null;
  text: string;
}

export interface AiSkill {
  name: string;
  category: string;
  proficiency: string | null;
}

export interface AiProfile {
  user_id: string;
  name: string;
  education: AiEducation[];
  experiences: AiExperience[];
  projects: AiProject[];
  skills: AiSkill[];
  achievements: AiAchievement[];
}

/** The subset of the Prisma types this module needs. Structural, so it accepts
 *  a Prisma result without importing the generated client -- which keeps this
 *  file testable without a database and without `prisma generate` having run. */
export interface ProfileRows {
  id: string;
  name: string | null;
  experiences: Array<{
    id: string;
    company: string;
    role: string;
    location: string | null;
    startDate: Date;
    endDate: Date | null;
    bullets: string[];
    detail: string | null;
  }>;
  projects: Array<{
    id: string;
    name: string;
    tech: string[];
    bullets: string[];
    detail: string | null;
    startDate: Date | null;
    endDate: Date | null;
  }>;
  achievements: Array<{ id: string; title: string; detail: string | null }>;
  education: Array<{
    id: string;
    institution: string;
    degree: string;
    field: string | null;
    gpa: number | null;
    startYear: number | null;
    endYear: number | null;
  }>;
  skills: Array<{ name: string; category: string; proficiency: string | null }>;
}

/**
 * `Date` to "YYYY-MM", in UTC.
 *
 * UTC on purpose. These are `@db.Date` columns with no time component, so
 * reading them in a local timezone behind UTC turns 2026-01-01 into December
 * 2025 -- an off-by-one-month on a resume, produced only for users west of
 * Greenwich, which is exactly the kind of bug that survives review.
 */
export function toYearMonth(date: Date | null | undefined): YearMonth {
  if (!date) return null;
  const month = String(date.getUTCMonth() + 1).padStart(2, "0");
  return `${date.getUTCFullYear()}-${month}`;
}

/** The one-line summary the education entry is matched against. */
function educationText(row: ProfileRows["education"][number]): string {
  const degree = row.field && !row.degree.includes(row.field) ? `${row.degree}, ${row.field}` : row.degree;
  return row.gpa === null ? degree : `${degree}, CGPA ${row.gpa.toFixed(2)}/10`;
}

/**
 * Build the pipeline's input document from one user's rows.
 *
 * Ordering is preserved from the caller's query rather than re-sorted here: the
 * Data Retriever scores by relevance and recency itself, and a second ordering
 * opinion in this layer would be one more thing to keep in step with it.
 */
export function toAiProfile(user: ProfileRows): AiProfile {
  return {
    user_id: user.id,
    name: user.name ?? "",
    education: user.education.map((row) => ({
      id: row.id,
      institution: row.institution,
      degree: row.degree,
      field: row.field,
      gpa: row.gpa,
      start_year: row.startYear,
      end_year: row.endYear,
      text: educationText(row),
    })),
    experiences: user.experiences.map((row) => ({
      id: row.id,
      company: row.company,
      role: row.role,
      location: row.location,
      start: toYearMonth(row.startDate),
      end: toYearMonth(row.endDate),
      // Derived from the absence of an end date, never stored: one fact, one
      // column. See the schema's note on `endDate`.
      current: row.endDate === null,
      bullets: row.bullets,
      detail: row.detail ?? "",
    })),
    projects: user.projects.map((row) => ({
      id: row.id,
      name: row.name,
      tech: row.tech,
      start: toYearMonth(row.startDate),
      current: row.endDate === null,
      bullets: row.bullets,
      detail: row.detail ?? "",
    })),
    skills: user.skills.map((row) => ({
      name: row.name,
      category: row.category,
      proficiency: row.proficiency,
    })),
    achievements: user.achievements.map((row) => ({
      id: row.id,
      title: row.title,
      text: row.detail ?? "",
    })),
  };
}

/** The Prisma `include` that fetches exactly what `toAiProfile` needs.
 *  Exported so a route cannot forget a relation and silently ship a profile with
 *  no projects -- which the pipeline would report as "no relevant experience". */
export const PROFILE_INCLUDE = {
  experiences: { orderBy: { startDate: "desc" } },
  projects: { orderBy: { startDate: "desc" } },
  achievements: { orderBy: { createdAt: "asc" } },
  education: { orderBy: { endYear: "desc" } },
  skills: { orderBy: { name: "asc" } },
} as const;
