"use client";

import type { Evidence } from "@/lib/types";

/**
 * "Here is what I found relevant, and why."
 *
 * The panel exists to make the pipeline's reasoning inspectable rather than
 * magical: every line is a profile item the user wrote, with the keywords it
 * matched. If the rewrite emphasises the wrong thing, this is where the user can
 * see it was matched for a defensible reason.
 */
export function EvidencePanel({ evidence }: { evidence: Evidence[] }) {
  if (evidence.length === 0) return null;
  return (
    <div className="panel">
      <h2>What was matched, and why</h2>
      <p className="hint">Ranked by relevance. Nothing outside this list can appear in the rewrite.</p>
      <table>
        <thead>
          <tr>
            <th>Item</th>
            <th>Kind</th>
            <th>Matched</th>
            <th>Score</th>
          </tr>
        </thead>
        <tbody>
          {evidence.map((item) => (
            <tr key={item.item_id}>
              <td>
                {item.title}
                {item.already_on_resume && <span className="muted"> · already on the resume</span>}
              </td>
              <td className="muted">{item.kind}</td>
              <td className="mono">{item.matched_keywords.slice(0, 6).join(", ")}</td>
              <td className="mono">{item.relevance.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
