"use client";

import Editor, { loader } from "@monaco-editor/react";
import { useEffect, useState } from "react";

/**
 * Monaco, loaded from this origin rather than a CDN.
 *
 * `@monaco-editor/react` fetches the editor from jsDelivr by default. That makes
 * the app unusable offline, adds a third-party origin to every page load, and
 * would be blocked outright by any reasonable CSP. The Dockerfile copies
 * monaco's `min/vs` into `public/monaco/vs`, and this points the loader there.
 */
loader.config({ paths: { vs: "/monaco/vs" } });

export function LatexEditor({
  value,
  onChange,
  height = 520,
}: {
  value: string;
  onChange: (next: string) => void;
  height?: number;
}) {
  // Monaco touches `window` on import, so it must not run during SSR.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  if (!mounted) {
    return (
      <div className="panel" style={{ height, display: "grid", placeItems: "center" }}>
        <span className="muted">Loading editor…</span>
      </div>
    );
  }

  return (
    <Editor
      height={height}
      // Monaco ships no LaTeX grammar, and a wrong grammar is worse than none:
      // highlighting LaTeX as C would colour `\section{...}` as a function call
      // and `%` as a modulus rather than a comment.
      defaultLanguage="plaintext"
      theme="vs-dark"
      value={value}
      onChange={(next) => onChange(next ?? "")}
      options={{
        minimap: { enabled: false },
        fontSize: 13,
        wordWrap: "on",
        // A resume is edited by hand here; automatic reformatting of LaTeX would
        // be actively wrong.
        formatOnPaste: false,
        formatOnType: false,
        scrollBeyondLastLine: false,
        renderWhitespace: "none",
      }}
      loading={<span className="muted">Loading editor…</span>}
    />
  );
}
