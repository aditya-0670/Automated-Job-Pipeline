"use client";

import { Fragment, useState } from "react";

import type { DiffSection } from "@/lib/types";

/**
 * The section-level diff.
 *
 * Sections, not lines, because that is the unit the user thinks in ("you changed
 * my Experience section"), and because LaTeX rewraps — a line-level diff reports
 * a reflowed paragraph as a rewrite and teaches the reader to skip it. Unchanged
 * sections are collapsed to a single row: they are the majority, and the point of
 * this screen is what moved.
 */
export function DiffView({ sections }: { sections: DiffSection[] }) {
  const [open, setOpen] = useState<string | null>(
    sections.find((s) => s.change !== "unchanged")?.section ?? null,
  );
  const changed = sections.filter((s) => s.change !== "unchanged");
  const unchanged = sections.filter((s) => s.change === "unchanged");

  return (
    <div>
      {/* Fixed layout: a unified diff has lines far wider than the viewport, and
          with the default auto layout the table grows to fit them and drags the
          whole page into horizontal scroll. Fixed widths bound the cell, so the
          diff scrolls inside its own box instead. */}
      <table className="fixed">
        <thead>
          <tr>
            <th>Section</th>
            <th>Change</th>
            <th>Similarity</th>
            <th>Lines</th>
          </tr>
        </thead>
        <tbody>
          {changed.map((section) => (
            <Fragment key={section.section}>
              <tr>
                <td>
                  <button
                    type="button"
                    onClick={() => setOpen(open === section.section ? null : section.section)}
                    style={{ border: "none", background: "none", padding: 0, color: "var(--accent)" }}
                  >
                    {section.section}
                  </button>
                </td>
                <td>
                  <span className={`tag ${section.change}`}>{section.change}</span>
                </td>
                <td className="mono">{(section.similarity * 100).toFixed(0)}%</td>
                <td className="mono">
                  {section.before_lines} → {section.after_lines}
                </td>
              </tr>
              {/* Its own full-width row rather than nested in the name cell: a
                  diff needs the whole table width, and inside a cell it would be
                  squeezed into one column. */}
              {open === section.section && section.diff && (
                <tr>
                  <td colSpan={4} style={{ padding: 0 }}>
                    <DiffBody text={section.diff} />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
      {unchanged.length > 0 && (
        <p className="muted" style={{ fontSize: 13, marginTop: 10 }}>
          {unchanged.length} section{unchanged.length === 1 ? "" : "s"} unchanged:{" "}
          {unchanged.map((s) => s.section).join(", ")}
        </p>
      )}
    </div>
  );
}

/** Colours a unified diff. Rendered from the server's text rather than
 *  recomputed here: the gateway already produced it, and a second diff
 *  implementation in the browser would be a second thing to disagree. */
function DiffBody({ text }: { text: string }) {
  return (
    <pre className="diff">
      {text.split("\n").map((line, index) => {
        const kind = line.startsWith("+")
          ? "add"
          : line.startsWith("-")
            ? "del"
            : line.startsWith("@@") || line.startsWith("---") || line.startsWith("+++")
              ? "meta"
              : "";
        return (
          <span key={index} className={kind}>
            {line}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}
