/**
 * Token issuance. The only thing `AUTH_MODE` changes.
 *
 * In `dev` this mints a token for the seeded user so the whole flow is drivable
 * from curl. In `strict` the route is gone -- and Google OAuth drops in here,
 * behind the same `signToken`, without touching the verifier.
 */

import { Router } from "express";

import { getConfig } from "../config.js";
import { requireAuth, signToken } from "../middleware/auth.js";
import { ApiError, asyncHandler } from "../middleware/errors.js";

import type { PrismaClient } from "@prisma/client";

export function authRouter(prisma: PrismaClient): Router {
  const router = Router();
  const config = getConfig();

  router.post(
    "/dev-token",
    asyncHandler(async (_req, res) => {
      if (config.AUTH_MODE !== "dev") {
        // 404, not 403: in strict mode this endpoint does not exist, and saying
        // so is more useful than hinting that it might with the right credential.
        throw ApiError.notFound("No such route");
      }
      const user = await prisma.user.findUnique({ where: { email: config.SEED_USER_EMAIL } });
      if (!user) {
        throw ApiError.notFound(
          `No user ${config.SEED_USER_EMAIL}. Run the seed first: make db-seed`,
        );
      }
      res.json({ token: signToken({ id: user.id, email: user.email }), user: { id: user.id, email: user.email } });
    }),
  );

  /** Who am I? The cheapest check that a token works. */
  router.get("/me", requireAuth, (req, res) => {
    res.json({ user: req.user });
  });

  return router;
}
