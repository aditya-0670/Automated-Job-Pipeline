import type { NextConfig } from "next";

const config: NextConfig = {
  // Standalone output: the runtime image then needs only .next/standalone and
  // the static assets, not node_modules -- the difference is roughly 400MB.
  output: "standalone",
  reactStrictMode: true,
  // The gateway is the only backend, and it is reached from the browser by its
  // public URL. No rewrites: proxying SSE through Next's dev server adds a
  // buffering layer between the pipeline and the browser for no benefit, and
  // the gateway already allows this origin explicitly.
};

export default config;
