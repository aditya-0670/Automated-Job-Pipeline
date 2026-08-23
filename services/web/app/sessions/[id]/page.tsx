"use client";

/**
 * One session, from extraction to PDF.
 *
 * The screen renders from `paused_at` rather than from a local step counter: the
 * pipeline is the authority on where a session is, and it can move backwards
 * (self-correction, "change keywords") in ways a counter here would get wrong. A
 * reload lands on exactly the right gate because the answer comes from the
 * checkpoint.
 */

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { EvidencePanel } from "@/components/EvidencePanel";
import { KeywordGate } from "@/components/KeywordGate";
import { Progress } from "@/components/Progress";
import { ReviewPanel } from "@/components/ReviewPanel";
import { confirmKeywords, ensureToken, pdfUrl, submitReview } from "@/lib/api";
import { useSession } from "@/lib/useSession";
import type { Keyword } from "@/lib/types";

export default function SessionPage() {
  const params = useParams<{ id: string }>();
  const sessionId = params?.id ?? null;
  const { status, events, error, working, refresh, resume } = useSession(sessionId);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  // The PDF is fetched by the browser itself (an iframe and a download link),
  // neither of which can send an Authorization header -- so the token goes in
  // the URL, and the URL cannot be built until the token exists.
  const [token, setToken] = useState<string | null>(null);
  useEffect(() => {
    void ensureToken().then(setToken).catch(() => setToken(null));
  }, []);

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setActionError(null);
    try {
      await fn();
      // The pipeline runs in the background, so the answer to "what now?" comes
      // from the stream and the next status read, not from this response. The
      // stream must be reopened explicitly: the server closed it when the
      // session paused for this very decision.
      resume();
      await refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const onConfirmKeywords = (selected: Keyword[]) =>
    // Sent whole: the pipeline re-ranks from the score and sources it gave us,
    // and stripping them to just the term would discard that.
    act(() => confirmKeywords(sessionId!, selected));

  return (
    <>
      {(error || actionError) && <div className="notice bad">{actionError ?? error}</div>}

      <Progress status={status} events={events} working={working} />

      {status?.paused_at === "keyword_review" && status.keyword_review && (
        <KeywordGate
          keywords={status.keyword_review.keywords}
          jobMetadata={status.keyword_review.job_metadata}
          onConfirm={onConfirmKeywords}
          busy={busy}
        />
      )}

      {status?.paused_at === "human_review" && status.human_review && (
        <ReviewPanel
          review={status.human_review}
          busy={busy}
          onDecision={(body) => act(() => submitReview(sessionId!, body))}
        />
      )}

      {status?.is_complete && status.pdf_ready && token && (
        <div className="panel">
          <h2>Your resume</h2>
          <div className="row" style={{ marginBottom: 12 }}>
            <a className="primary" href={pdfUrl(sessionId!, token)} download data-testid="download">
              <button className="primary" type="button">
                Download PDF
              </button>
            </a>
            <span className="muted">
              Compiled from your own template — same fonts, same macros, same layout.
            </span>
          </div>
          {/* An iframe rather than a JS PDF renderer: the browser already has
              one, and shipping pdf.js to preview a one-page document is a large
              dependency for no gain. */}
          <iframe className="pdf" src={pdfUrl(sessionId!, token)} title="Compiled resume" />
        </div>
      )}

      {status?.step === "FAILED" && (
        <div className="panel">
          <div className="notice bad">
            <strong>The pipeline stopped.</strong> {status.error}
          </div>
          <p className="muted">
            Nothing was lost: the session is checkpointed, so retrying resumes from the last
            completed step rather than starting over.
          </p>
        </div>
      )}

      {/* Shown from the matching step onwards, at every later gate: "why is this
          on my resume?" is a question the user asks while reading the diff, not
          only when the pipeline happens to be idle. */}
      {status?.evidence && status.evidence.length > 0 && (
        <EvidencePanel evidence={status.evidence} />
      )}
    </>
  );
}
