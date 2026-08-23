-- AlterTable
ALTER TABLE "users" ADD COLUMN     "githubReposEtag" TEXT,
ADD COLUMN     "githubSyncedAt" TIMESTAMP(3),
ADD COLUMN     "githubUsername" TEXT;

