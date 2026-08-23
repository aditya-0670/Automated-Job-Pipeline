"use client";

/**
 * The profile: the evidence set, and the template.
 *
 * Read-mostly on purpose. The pipeline's honesty guarantee is that it can only
 * use what is here, so the valuable thing this page does is *show* the user
 * exactly what that is — the same document the AI service receives, field for
 * field. Editing every entity from the browser is Part 12/13 API surface that
 * exists and can be driven from curl; what the user cannot get anywhere else is
 * this view.
 */

import { useEffect, useState } from "react";

import { LatexEditor } from "@/components/LatexEditor";
import {
  getGitHubStatus,
  getLatexTemplate,
  getProfile,
  putLatexTemplate,
  syncGitHub,
} from "@/lib/api";
import type { Profile } from "@/lib/types";

export default function ProfilePage() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [latex, setLatex] = useState("");
  const [savedLatex, setSavedLatex] = useState("");
  const [github, setGithub] = useState<Awaited<ReturnType<typeof getGitHubStatus>> | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const [{ profile: loaded }, template, gh] = await Promise.all([
        getProfile(),
        getLatexTemplate(),
        getGitHubStatus().catch(() => null),
      ]);
      setProfile(loaded);
      setLatex(template);
      setSavedLatex(template);
      setGithub(gh);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const { bytes } = await putLatexTemplate(latex);
      setSavedLatex(latex);
      setMessage(`Template saved (${bytes.toLocaleString()} bytes).`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function sync() {
    setBusy(true);
    setError(null);
    try {
      const result = await syncGitHub(false);
      setMessage(
        result.status === "fresh"
          ? "Already up to date — no GitHub API calls were made."
          : `Synced: ${result.created} new, ${result.updated} updated (${result.apiRequests} API requests).`,
      );
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  if (error && !profile) return <div className="notice bad">{error}</div>;
  if (!profile) return <p className="muted">Loading…</p>;

  const grouped = profile.skills.reduce<Record<string, string[]>>((acc, skill) => {
    (acc[skill.category] ??= []).push(skill.name);
    return acc;
  }, {});

  return (
    <>
      {error && <div className="notice bad">{error}</div>}
      {message && <div className="notice good">{message}</div>}

      <div className="panel">
        <h2>{profile.name}</h2>
        <p className="hint">
          This is the whole evidence set. The Refactorer is given only what is on this page, and the
          Evaluator rejects any claim that cannot be traced back to it.
        </p>
        <div className="split">
          <div>
            <h3 style={{ fontSize: 14 }}>Experience</h3>
            {profile.experiences.map((exp) => (
              <div key={exp.id} style={{ marginBottom: 12 }}>
                <strong>{exp.role}</strong> · {exp.company}{" "}
                <span className="muted mono">
                  {exp.start} → {exp.current ? "present" : exp.end}
                </span>
                <ul style={{ margin: "4px 0 0 18px", fontSize: 14 }}>
                  {exp.bullets.map((bullet, index) => (
                    <li key={index}>{bullet}</li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
          <div>
            <h3 style={{ fontSize: 14 }}>Projects</h3>
            {profile.projects.map((project) => (
              <div key={project.id} style={{ marginBottom: 12 }}>
                <strong>{project.name}</strong>{" "}
                <span className="muted mono">{project.tech.slice(0, 5).join(", ")}</span>
                <ul style={{ margin: "4px 0 0 18px", fontSize: 14 }}>
                  {project.bullets.slice(0, 3).map((bullet, index) => (
                    <li key={index}>{bullet}</li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </div>

        <h3 style={{ fontSize: 14, marginTop: 18 }}>Skills</h3>
        {Object.entries(grouped).map(([category, names]) => (
          <p key={category} style={{ margin: "2px 0", fontSize: 14 }}>
            <span className="muted">{category}:</span> {names.join(", ")}
          </p>
        ))}
      </div>

      <div className="panel">
        <h2>GitHub</h2>
        {github?.connected ? (
          <>
            <p className="hint">
              {github.username ? `@${github.username} · ` : ""}
              {github.syncedProjects} synced project{github.syncedProjects === 1 ? "" : "s"}
              {github.lastSyncedAt
                ? ` · last synced ${new Date(github.lastSyncedAt).toLocaleString()}`
                : ""}
              {github.fresh ? " · fresh" : ""}
            </p>
            <button onClick={sync} disabled={busy}>
              {busy ? "Syncing…" : "Sync now"}
            </button>
          </>
        ) : (
          <p className="hint">
            No token stored. Add one with{" "}
            <span className="mono">PUT /api/profile/github/token</span> — a classic PAT with read
            access is enough. It is encrypted before it is stored.
          </p>
        )}
      </div>

      <div className="panel">
        <h2>Your LaTeX template</h2>
        <p className="hint">
          The rewrite preserves this file&apos;s preamble, packages and macros exactly — only the
          content inside your sections changes.
        </p>
        <LatexEditor value={latex} onChange={setLatex} height={420} />
        <div className="row" style={{ marginTop: 12 }}>
          <button className="primary" onClick={save} disabled={busy || latex === savedLatex}>
            {busy ? "Saving…" : "Save template"}
          </button>
          {latex !== savedLatex && <span className="muted">unsaved changes</span>}
        </div>
      </div>
    </>
  );
}
