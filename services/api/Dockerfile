# ResumeForge API gateway.
#
# Multi-stage, and the stages exist for two reasons: the build needs devDeps and
# the Prisma generator, and the runtime must not ship either. The final image
# carries production dependencies, the generated client and compiled JavaScript
# -- no TypeScript, no compiler, no test tooling.

# ── deps ──────────────────────────────────────────────────────────────────
FROM node:22-slim AS deps
WORKDIR /app
# Copied alone so a source edit does not invalidate the dependency layer.
COPY package.json package-lock.json ./
# `npm ci` and not `install`: the lockfile is the input, and a build that can
# silently resolve a different version is not reproducible.
RUN npm ci --no-audit --no-fund

# ── build ─────────────────────────────────────────────────────────────────
FROM deps AS build
WORKDIR /app
# The generator reads the installed OpenSSL to decide which engine to emit; see
# the binaryTargets note in schema.prisma.
RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*
COPY prisma ./prisma
# The client is generated from the schema, so it must exist before tsc runs --
# every `import type { PrismaClient }` depends on it.
RUN npx prisma generate
COPY tsconfig.json tsconfig.build.json ./
COPY src ./src
RUN npx tsc -p tsconfig.build.json

# ── runtime ───────────────────────────────────────────────────────────────
FROM node:22-slim AS runtime
# openssl is Prisma's runtime requirement for the query engine; without it the
# client warns and falls back to a guessed binary.
RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV NODE_ENV=production

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
# The generated client lives in node_modules, so it is copied from the build
# stage rather than regenerated -- which would need the Prisma CLI here.
COPY --from=build /app/node_modules/.prisma ./node_modules/.prisma
COPY --from=build /app/node_modules/@prisma/client ./node_modules/@prisma/client
COPY --from=build /app/dist ./dist
# Migrations and the seed ship with the image so a deploy can apply them without
# a checkout (Part 20 runs `migrate deploy` as a release step).
COPY prisma ./prisma

# node:22-slim already provides an unprivileged `node` user; creating another
# would just be a different uid with the same properties.
USER node

EXPOSE 4000
# No shell form: with `CMD node ...` the process is PID 1 and receives SIGTERM
# directly, which is what makes the graceful shutdown in src/index.ts run.
CMD ["node", "dist/index.js"]

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
  CMD node -e "fetch('http://127.0.0.1:4000/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
