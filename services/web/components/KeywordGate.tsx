"use client";

import { useState } from "react";

import type { Keyword } from "@/lib/types";

/**
 * Extraction Layer 4: the user confirms the keywords before anything is
 * generated.
 *
 * This screen exists because the three automated layers are deterministic but
 * not omniscient — they rank "Communication" alongside "Kubernetes" because the
 * posting mentions both. Deselecting is cheaper here than regenerating a resume
 * afterwards, and the pipeline is *paused* at this point, so nothing has been
 * spent yet.
 */
export function KeywordGate({
  keywords,
  jobMetadata,
  onConfirm,
  busy,
}: {
  keywords: Keyword[];
  jobMetadata: Record<string, string>;
  onConfirm: (selected: Keyword[]) => void;
  busy: boolean;
}) {
  const [dropped, setDropped] = useState<Set<string>>(new Set());
  const [added, setAdded] = useState<Keyword[]>([]);
  const [draft, setDraft] = useState("");

  const all = [...keywords, ...added];
  const kept = all.filter((k) => !dropped.has(k.term));

  function toggle(term: string) {
    setDropped((current) => {
      const next = new Set(current);
      if (next.has(term)) next.delete(term);
      else next.add(term);
      return next;
    });
  }

  function add() {
    const term = draft.trim();
    if (!term) return;
    // The score puts a hand-added keyword above everything extracted: the user
    // typed it because the extractor missed something that matters to them.
    if (!all.some((k) => k.term.toLowerCase() === term.toLowerCase())) {
      setAdded((current) => [...current, { term, score: 99, sources: ["user"] }]);
    }
    setDraft("");
  }

  return (
    <div className="panel">
      <h2>Confirm the keywords</h2>
      <p className="hint">
        {/* A pasted description has no metadata, so the posting is described
            rather than named -- the earlier version read "this posting. Click…"
            mid-sentence whenever the title was missing. */}
        {[jobMetadata.title, jobMetadata.company].filter(Boolean).join(" — ") ||
          "From the description you pasted"}
        . Click a keyword to drop it. Nothing has been generated yet, so this is the cheap place to
        correct the extraction.
      </p>

      <div className="chips">
        {all.map((keyword) => {
          const off = dropped.has(keyword.term);
          return (
            <span
              key={keyword.term}
              className={`chip ${off ? "off" : "on"}`}
              onClick={() => toggle(keyword.term)}
              role="button"
              aria-pressed={!off}
              data-testid="keyword-chip"
            >
              {keyword.term}
              <small>{keyword.score.toFixed(0)}</small>
            </span>
          );
        })}
      </div>

      <div className="row" style={{ marginTop: 16 }}>
        <input
          type="text"
          placeholder="Add a keyword the extractor missed"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              add();
            }
          }}
          style={{ maxWidth: 340 }}
        />
        <button onClick={add} type="button">
          Add
        </button>
        <span className="spacer" />
        <span className="muted">
          {kept.length} of {all.length} kept
        </span>
        <button
          className="primary"
          disabled={busy || kept.length === 0}
          onClick={() => onConfirm(kept)}
          data-testid="confirm-keywords"
        >
          {busy ? "Starting…" : "Generate resume"}
        </button>
      </div>
      {kept.length === 0 && (
        <div className="notice bad" style={{ marginTop: 12 }}>
          At least one keyword is needed to match against your profile.
        </div>
      )}
    </div>
  );
}
