/** One PrismaClient for the process. A client per request exhausts the
 *  connection pool under any real load, and Prisma's own guidance is one
 *  instance per application. */

import { PrismaClient } from "@prisma/client";

import { getConfig } from "./config.js";
import { logger } from "./logger.js";

let client: PrismaClient | undefined;

export function getPrisma(): PrismaClient {
  if (!client) {
    client = new PrismaClient({ datasources: { db: { url: getConfig().DATABASE_URL } } });
    logger.debug("Prisma client created");
  }
  return client;
}

export async function disconnectPrisma(): Promise<void> {
  await client?.$disconnect();
  client = undefined;
}
