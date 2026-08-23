# AWS deployment

**Status: written, not run.** There is no AWS account behind this repository, so
every file here is unverified against real EC2. What *is* verified is stated as
such below, and what is not is not dressed up. The honest summary: the compose
file, the Caddyfile and the workflow are complete and lint clean; nothing has
ever booted on an instance.

## The instance

t3.micro, Amazon Linux 2023, 20GB gp3. `user-data.sh` runs once on first boot
and does only what cannot be done later or is needed before the first deploy:
installs Docker and the compose plugin, creates the `deploy` user, caps the log
driver, and adds 2GB of swap.

The swap matters more than it looks. A t3.micro has 1GB of RAM and the AI
service alone requests 512MB; without swap the kernel OOM-kills whichever
container most recently asked for memory, which is rarely the one at fault, and
the symptom is a service dying with no error anywhere in its own logs.

## Security group

Three inbound rules and no more:

| Port | Source | Why |
|---|---|---|
| 80 | 0.0.0.0/0 | HTTP, and the ACME challenge Caddy needs to issue a certificate |
| 443 | 0.0.0.0/0 | HTTPS |
| 22 | your IP only | SSH, for the deploy and for looking at things |

Nothing else is exposed. Postgres, Redis and the AI service have no host ports
in `docker-compose.prod.yml` at all, so they are unreachable from outside the
compose network regardless of what the security group says — the two controls
agree, which is the point.

SSH from `0.0.0.0/0` is the one to resist. It is the difference between "an
attacker needs my key" and "an attacker needs my key and my IP", and it costs
nothing to narrow.

## Deploying

`.github/workflows/deploy.yml` runs on every push to `main`:

```
build (matrix: ai, api, web) → push to GHCR → scp compose + Caddyfile
  → write .env → pull → migrate → up -d → health gate → rollback on failure
```

Images are built in Actions and never on the instance: a build on a t3.micro
competes for memory with the application it is trying to replace.

Every image in a deploy carries the same tag — the commit SHA — so "what is
running?" has exactly one answer, and a rollback is that one variable changed
back. The rollback target is read *from the box* rather than inferred from git
history, because the box is the only thing that knows what actually survived the
last deploy.

Rolling back by hand is the same workflow with `image_tag` set to a previous
SHA; the build job is skipped and the images are already on the instance, so it
is a container restart rather than a download.

### Required secrets

`EC2_HOST`, `EC2_SSH_KEY`, `POSTGRES_PASSWORD`, `INTERNAL_API_KEY`,
`JWT_SECRET`, `ENCRYPTION_KEY`. Optional: `GEMINI_API_KEY` (without it the
pipeline runs on the deterministic mock provider — the site works, the rewrites
are stubs), `DOMAIN` and `PUBLIC_URL`.

`PUBLIC_URL` is a *build* argument, not a runtime one: `NEXT_PUBLIC_*` values are
inlined into the client bundle when the image is built, so changing the domain
requires a rebuild and not a restart. That is the one genuinely surprising thing
about deploying a Next.js app and it is worth knowing before it costs an hour.

## TLS

Caddy, chosen over nginx for one reason: it obtains and renews certificates
itself. nginx plus certbot plus a renewal timer is three moving parts doing what
this does in one line, and a forgotten renewal takes the site down ninety days
later, on a weekend.

Caddy will not issue a certificate for a bare IP address. Without a domain,
`DOMAIN` is unset and the site serves plain HTTP on port 80.

## What would need checking on a first real deploy

Listed because "it should work" is not the same as "it worked", and this is the
list I would work through:

1. `user-data.sh` completes — `/var/log/resumeforge-bootstrap.done` exists, and
   `cloud-init` logged no failure.
2. The `deploy` user can run `docker` without `sudo` (the group membership only
   takes effect on a new login session).
3. GHCR pull works from the instance. A private package needs a token with
   `read:packages`; the workflow logs in with `GITHUB_TOKEN`, which is scoped to
   the workflow run and not to the box.
4. The health gate's 60 seconds is enough on a t3.micro. The AI service builds a
   492-pattern automaton and runs checkpoint migrations at boot, and that boot is
   measured on a laptop, not on two burstable vCPUs.
5. Memory. Five containers on 1GB is tight even with swap; if it is not enough,
   the AI service is the one to move to its own instance, because it is the one
   with a real memory floor.
