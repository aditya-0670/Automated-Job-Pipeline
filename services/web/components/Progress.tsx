"use client";

import type { ProgressEvent, SessionStatus } from "@/lib/types";

/**
 * The progress indicator, driven by the same `Step` enum the graph uses.
 *
 * `progress` comes from the pipeline rather than being counted here: the loop
 * steps (CORRECTING, REFINING) hold their parent's position, so a self-correction
 * does not make the bar go backwards, and only the graph knows that.
 */
export function Progress({
  status,
  events,
  working,
}: {
  status: SessionStatus | null;
  events: ProgressEvent[];
  working: boolean;
}) {
  const fraction = status?.progress ?? 0;
  const label = status?.label ?? "Starting";

  return (
    <div className="panel">
      <div className="row">
        <strong>{label}</strong>
        {working && <span className="muted">working…</span>}
        <span className="spacer" />
        <span className="muted mono">{Math.round(fraction * 100)}%</span>
      </div>
      <div className="bar" style={{ marginTop: 10 }}>
        <span style={{ width: `${Math.max(3, fraction * 100)}%` }} />
      </div>

      {events.length > 0 && (
        <div className="log">
          {events.map((event) => (
            <div key={event.sequence}>
              <span className="step">{event.label}</span>
              {event.detail && <span> — {event.detail}</span>}
              {typeof event.data?.input_tokens === "number" && (
                <span className="muted">
                  {" "}
                  ({event.data.input_tokens as number} in / {String(event.data.output_tokens)} out)
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {status?.warnings?.map((warning) => (
        <div className="notice" key={warning} style={{ marginTop: 12 }}>
          {warning}
        </div>
      ))}
    </div>
  );
}
