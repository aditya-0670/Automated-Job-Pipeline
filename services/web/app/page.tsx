"use client";

/**
 * The start screen: a job posting in, a session out.
 *
 * One flow, deliberately. This is not a chat clone — the product does one thing,
 * and a text box with a "send" button would imply the model is free to do
 * anything, when in fact the pipeline is a fixed graph with two gates.
 */

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { ApiError, createSession, getProfile, listSessions } from "@/lib/api";

export default function StartPage() {
  const router = useRouter();
  const [jobUrl, setJobUrl] = useState("");
  const [jobText, setJobText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState<{ hasLatex: boolean; evidence: number } | null>(null);
  const [recent, setRecent] = useState<
    { id: string; jobUrl: string | null; status: string; currentStep: string }[]
  >([]);

  // Checked up front so the user is told what is missing *before* pasting a job
  // description, rather than after.
  useEffect(() => {
    void (async () => {
      try {
        const { profile, hasLatexTemplate } = await getProfile();
        setReady({
          hasLatex: hasLatexTemplate,
          evidence: profile.experiences.length + profile.projects.length,
        });
        const { sessions } = await listSessions();
        setRecent(sessions.slice(0, 5));
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    })();
  }, []);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      const body = jobUrl.trim() ? { jobUrl: jobUrl.trim() } : { jobText: jobText.trim() };
      const { sessionId } = await createSession(body);
      router.push(`/sessions/${sessionId}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setBusy(false);
    }
  }

  const canStart = jobUrl.trim().length > 0 || jobText.trim().length >= 200;

  return (
    <>
      {error && <div className="notice bad">{error}</div>}

      {ready && !ready.hasLatex && (
        <div className="notice">
          You have no LaTeX template yet. <a href="/profile">Add one on the profile page</a> — the
          rewrite preserves your own template rather than generating a new design.
        </div>
      )}
      {ready && ready.evidence === 0 && (
        <div className="notice">
          Your profile has no experiences or projects. The pipeline can only use evidence you have
          given it, so there would be nothing to draw on.
        </div>
      )}

      <div className="panel">
        <h2>Tailor your resume to a posting</h2>
        <p className="hint">
          Paste a job URL, or the description text if the site blocks scraping. Keyword extraction
          runs without an LLM, so nothing is spent before you confirm the keywords.
        </p>

        <label htmlFor="jobUrl">Job posting URL</label>
        <input
          id="jobUrl"
          type="url"
          placeholder="https://careers.example.com/jobs/12345"
          value={jobUrl}
          onChange={(event) => setJobUrl(event.target.value)}
          data-testid="job-url"
        />

        <p className="muted" style={{ margin: "14px 0 6px", fontSize: 13 }}>
          or paste the description
        </p>
        <textarea
          value={jobText}
          onChange={(event) => setJobText(event.target.value)}
          placeholder="Paste at least a couple of paragraphs of the posting…"
          data-testid="job-text"
        />

        <div className="row" style={{ marginTop: 14 }}>
          <button className="primary" disabled={!canStart || busy} onClick={start} data-testid="start">
            {busy ? "Starting…" : "Extract keywords"}
          </button>
          {!canStart && jobText.length > 0 && (
            <span className="muted">
              {200 - jobText.trim().length} more characters, or use a URL
            </span>
          )}
        </div>
      </div>

      {recent.length > 0 && (
        <div className="panel">
          <h2>Recent sessions</h2>
          <table>
            <tbody>
              {recent.map((session) => (
                <tr key={session.id}>
                  <td>
                    <a href={`/sessions/${session.id}`}>{session.jobUrl ?? "pasted description"}</a>
                  </td>
                  <td className="muted">{session.currentStep}</td>
                  <td className="muted">{session.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
