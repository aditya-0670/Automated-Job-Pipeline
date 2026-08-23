# ResumeForge web.
#
# `output: "standalone"` in next.config.ts is what makes the runtime stage small:
# Next traces the modules actually reached and emits a self-contained server, so
# the final image carries no node_modules tree at all.

# ── deps ──────────────────────────────────────────────────────────────────
FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# ── build ─────────────────────────────────────────────────────────────────
FROM deps AS build
WORKDIR /app
COPY tsconfig.json next.config.ts ./
COPY app ./app
COPY lib ./lib
COPY components ./components

# Monaco is served from this origin rather than a CDN: @monaco-editor/react
# fetches it from jsDelivr by default, which breaks offline use, adds a
# third-party origin to every page load, and would be blocked by any reasonable
# CSP. `loader.config` in components/LatexEditor.tsx points at this copy.
RUN mkdir -p public/monaco && cp -r node_modules/monaco-editor/min/vs public/monaco/vs

# NEXT_PUBLIC_* values are inlined into the client bundle at build time, so the
# gateway's browser-facing URL is a build argument, not a runtime env var. This
# is the one thing about a Next.js image that surprises people.
ARG NEXT_PUBLIC_API_URL=http://localhost:4000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ── runtime ───────────────────────────────────────────────────────────────
FROM node:22-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

# Three copies, and the split is Next's: the traced server, the static assets it
# does not serve itself, and public/ (which now holds Monaco).
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public

USER node
EXPOSE 3000
CMD ["node", "server.js"]

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
