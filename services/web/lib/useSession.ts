"use client";

/**
 * One hook for "where is this session, and what is it doing?".
 *
 * The design point: **the stream is for progress, the status endpoint is for
 * truth.** Events tell the user something is happening; the full state — the
 * keyword list, the diff, the warnings — is fetched when the pipeline stops.
 * Trying to reconstruct state from the event log would mean a reloaded page
 * showed less than a fresh one, and the checkpoint already holds the answer.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ensureToken, getSession, streamUrl } from "./api";
import type { ProgressEvent, SessionStatus } from "./types";

export interface SessionState {
  status: SessionStatus | null;
  events: ProgressEvent[];
  error: string | null;
  /** True while the pipeline is working: neither finished nor waiting on us. */
  working: boolean;
  refresh: () => Promise<void>;
  /**
   * Reopen the stream because the session was just resumed.
   *
   * The server ends the stream at every pause — correctly, since it is waiting
   * on a person and holding the connection open would be waiting too. So once
   * the person answers, there is no stream any more and nothing to reopen it:
   * without this the page sat at the keyword gate forever while the pipeline
   * finished behind it. Answering a gate is exactly the moment a new stream is
   * needed.
   */
  resume: () => void;
}

export function useSession(sessionId: string | null): SessionState {
  const [status, setStatus] = useState<SessionStatus | null>(null);
  const [events, setEvents] = useState<ProgressEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [working, setWorking] = useState(true);
  // A ref, not state: the reconnect logic reads it inside a callback that must
  // not be re-created on every event.
  const closed = useRef(false);
  // Bumped when the session is resumed, which re-runs the effect and opens a
  // fresh EventSource.
  const [streamKey, setStreamKey] = useState(0);
  // The last sequence handed to the UI, read when a stream is opened. A ref
  // because the effect must not re-run on every event.
  const lastSequence = useRef(0);

  const refresh = useCallback(async () => {
    if (!sessionId) return;
    try {
      const next = await getSession(sessionId);
      setStatus(next);
      setWorking(!next.is_paused && !next.is_complete);
      setError(next.error);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId) return;
    closed.current = false;
    let source: EventSource | null = null;
    let cancelled = false;

    // The token has to exist *before* the stream is opened. `EventSource` cannot
    // set headers, so the token rides in the query string -- and on a cold load
    // (a shared session link, a new browser) `ensureToken` has not resolved when
    // the first render happens, so a synchronous read would open the stream with
    // an empty token and get a silent 401 that never retries.
    void (async () => {
      try {
        await ensureToken();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        return;
      }
      if (cancelled) return;
      await refresh();
      if (cancelled) return;

      source = new EventSource(streamUrl(sessionId, lastSequence.current));

      source.addEventListener("progress", (event) => {
        const data = JSON.parse((event as MessageEvent<string>).data) as ProgressEvent;
        // Deduplicated by sequence: a reconnect replays from the last id it saw,
        // and React 19's strict mode mounts effects twice in development.
        lastSequence.current = Math.max(lastSequence.current, data.sequence);
        setEvents((current) =>
          current.some((e) => e.sequence === data.sequence) ? current : [...current, data],
        );
        setWorking(true);
      });

      // The pipeline stopping is the signal to fetch real state. `paused` means
      // it is waiting for the user, `done` means it finished or failed -- both
      // end the stream server-side, so there is nothing to close by hand.
      const finish = () => {
        closed.current = true;
        source?.close();
        void refresh();
      };
      source.addEventListener("paused", finish);
      source.addEventListener("done", finish);
      source.addEventListener("error", (event) => {
        const payload = ((event as MessageEvent<string>).data ?? "") || "{}";
        const message = JSON.parse(payload) as { message?: string };
        if (message.message) setError(message.message);
      });

      source.onerror = () => {
        // EventSource reconnects on its own, so this is only worth acting on
        // once the stream is finished -- otherwise a normal reconnect would be
        // reported to the user as a failure.
        if (closed.current) source?.close();
      };
    })();

    return () => {
      cancelled = true;
      closed.current = true;
      source?.close();
    };
  }, [sessionId, refresh, streamKey]);

  const resume = useCallback(() => {
    setWorking(true);
    setStreamKey((key) => key + 1);
  }, []);

  return { status, events, error, working, refresh, resume };
}
