"use client";

import { useState } from "react";

import { DiffView } from "./DiffView";
import { LatexEditor } from "./LatexEditor";
import type { ReviewPayload } from "@/lib/types";

/**
 * The human review gate: approve, ask for changes, or edit by hand.
 *
 * Unresolved problems are shown *above* the approve button, not tucked into a
 * details pane. The pipeline degrades gracefully — it hands over a resume with
 * warnings attached rather than looping forever — and the moment the user signs
 * off is exactly when they need to see what was not fixed.
 */
export function ReviewPanel({
  review,
  onDecision,
  busy,
}: {
  review: ReviewPayload;
  onDecision: (body: { decision: string; changeRequest?: string; editedLatex?: string }) => void;
  busy: boolean;
}) {
  const [mode, setMode] = useState<"diff" | "edit">("diff");
  const [instruction, setInstruction] = useState("");
  const [latex, setLatex] = useState(review.latex);

  const blocking =
    review.unresolved.factual_errors.length + review.unresolved.structural_errors.length;

  return (
    <div className="panel">
      <h2>Review the rewrite</h2>
      <p className="hint">
        {review.summary.changed} of {review.summary.total_sections} sections changed.
      </p>

      {blocking > 0 && (
        <div className="notice bad" data-testid="unresolved">
          <strong>The checks did not pass, and this is your last chance to see why.</strong>
          <ul style={{ margin: "6px 0 0 18px", padding: 0 }}>
            {[...review.unresolved.factual_errors, ...review.unresolved.structural_errors].map(
              (problem) => (
                <li key={problem}>{problem}</li>
              ),
            )}
          </ul>
        </div>
      )}

      {review.unsupported_keywords.length > 0 && (
        <div className="notice">
          <strong>The posting asks for these and your profile cannot evidence them:</strong>{" "}
          {review.unsupported_keywords.join(", ")}. They were deliberately left off — claiming
          them would be the hallucination this pipeline exists to prevent.
        </div>
      )}

      <div className="row" style={{ marginBottom: 14 }}>
        <button type="button" onClick={() => setMode("diff")} className={mode === "diff" ? "primary" : ""}>
          What changed
        </button>
        <button type="button" onClick={() => setMode("edit")} className={mode === "edit" ? "primary" : ""}>
          Edit the LaTeX
        </button>
      </div>

      {mode === "diff" ? (
        <DiffView sections={review.sections} />
      ) : (
        <LatexEditor value={latex} onChange={setLatex} />
      )}

      {review.changelog.length > 0 && mode === "diff" && (
        <details style={{ marginTop: 14 }}>
          <summary className="muted">Why each change was made ({review.changelog.length})</summary>
          <ul style={{ fontSize: 14 }}>
            {review.changelog.map((entry, index) => (
              <li key={index}>
                <strong>{entry.section ?? "resume"}</strong>: {entry.reason ?? entry.change_type}
              </li>
            ))}
          </ul>
        </details>
      )}

      <hr style={{ border: 0, borderTop: "1px solid var(--border)", margin: "18px 0" }} />

      <label htmlFor="instruction">Ask for a change instead (optional)</label>
      <input
        id="instruction"
        type="text"
        placeholder="e.g. Lead the Oracle role with the debugging work"
        value={instruction}
        onChange={(event) => setInstruction(event.target.value)}
      />

      <div className="row" style={{ marginTop: 14 }}>
        <button
          className="primary"
          disabled={busy}
          onClick={() => onDecision({ decision: "accept" })}
          data-testid="accept"
        >
          Approve and compile
        </button>
        <button
          disabled={busy || instruction.trim().length < 3}
          onClick={() => onDecision({ decision: "request_changes", changeRequest: instruction })}
        >
          Request changes
        </button>
        <button
          disabled={busy || latex === review.latex}
          onClick={() => onDecision({ decision: "edit", editedLatex: latex })}
          title="Your edit is re-checked by the guardrails before it compiles"
        >
          Use my edits
        </button>
        <span className="spacer" />
        <button
          disabled={busy}
          onClick={() => onDecision({ decision: "modify_keywords" })}
          title="Go back to the keyword step and start the rewrite again"
        >
          Change keywords
        </button>
      </div>
    </div>
  );
}
