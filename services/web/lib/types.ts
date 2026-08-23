/** The shapes the gateway returns. Hand-written rather than generated: there is
 *  one consumer and one producer, and a generator would be a build step to
 *  maintain for four screens. */

export interface AiSkill {
  name: string;
  category: string;
  proficiency: string | null;
}

export interface AiExperience {
  id: string;
  company: string;
  role: string;
  location: string | null;
  start: string | null;
  end: string | null;
  current: boolean;
  bullets: string[];
  detail: string;
}

export interface AiProject {
  id: string;
  name: string;
  tech: string[];
  start: string | null;
  current: boolean;
  bullets: string[];
  detail: string;
}

export interface Profile {
  user_id: string;
  name: string;
  experiences: AiExperience[];
  projects: AiProject[];
  skills: AiSkill[];
  achievements: { id: string; title: string; text: string }[];
  education: { id: string; institution: string; degree: string; text: string }[];
}

export interface Keyword {
  term: string;
  category?: string;
  score: number;
  sources?: string[];
  section?: string;
}

export interface Evidence {
  item_id: string;
  kind: string;
  title: string;
  matched_keywords: string[];
  relevance: number;
  already_on_resume: boolean;
}

export interface DiffSection {
  section: string;
  change: "unchanged" | "modified" | "added" | "removed";
  similarity: number;
  before_lines: number;
  after_lines: number;
  diff?: string;
}

export interface ReviewPayload {
  sections: DiffSection[];
  summary: { total_sections: number; changed: number; modified: number; added: number; removed: number };
  changelog: { section?: string; change_type?: string; reason?: string }[];
  warnings: string[];
  unresolved: { factual_errors: string[]; structural_errors: string[] };
  quality: Record<string, unknown>;
  suggestions: unknown[];
  unsupported_keywords: string[];
  latex: string;
}

export interface SessionStatus {
  session_id: string;
  step: string;
  label: string;
  progress: number;
  is_paused: boolean;
  paused_at: string | null;
  is_complete: boolean;
  error: string | null;
  warnings: string[];
  iteration_count: number;
  pdf_ready: boolean;
  evidence?: Evidence[];
  keyword_review?: {
    keywords: Keyword[];
    by_category: Record<string, string[]>;
    job_metadata: Record<string, string>;
    scrape_tier: string;
    stats: Record<string, unknown>;
  };
  human_review?: ReviewPayload;
}

export interface ProgressEvent {
  sequence: number;
  step: string;
  label: string;
  detail: string;
  progress: number;
  data: Record<string, unknown>;
}
