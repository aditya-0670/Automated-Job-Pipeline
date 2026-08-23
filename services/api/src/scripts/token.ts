/** Print a dev token, for driving the API from curl:
 *
 *     TOKEN=$(npm run -s token)
 *     curl -H "Authorization: Bearer $TOKEN" localhost:4000/api/profile
 */

import { getConfig } from "../config.js";
import { signToken } from "../middleware/auth.js";
import { disconnectPrisma, getPrisma } from "../db.js";

const prisma = getPrisma();
const user = await prisma.user.findUnique({ where: { email: getConfig().SEED_USER_EMAIL } });
if (!user) {
  console.error(`No seeded user ${getConfig().SEED_USER_EMAIL}. Run: make db-seed`);
  process.exit(1);
}
console.log(signToken({ id: user.id, email: user.email }));
await disconnectPrisma();
