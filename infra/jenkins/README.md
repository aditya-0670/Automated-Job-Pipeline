# Jenkins

The same pipeline as `.github/workflows/ci.yml`, in the tool most enterprises
actually run. Having both is the point: the repository lives on GitHub, and the
job description that motivated this project lists Jenkins.

```bash
make ci-up        # build the image, start the controller  → http://localhost:8080
make ci-build     # trigger a build and wait for the result
make ci-logs      # follow the controller log
make ci-reset     # delete the volume, so the next start reconfigures from scratch
```

Credentials default to `admin` / `admin` (`JENKINS_ADMIN_*` in `.env`).

## What is configured, and where

Nothing is configured by clicking. `casc.yaml` (JCasC) owns the security realm,
the authorization strategy, the credential the pipeline binds, and the job
definition; `plugins.txt` pins the plugin set; `Dockerfile` adds the Docker CLI.
`make ci-reset && make ci-up` reproduces the controller exactly. **If you change
something in the UI, it is gone on the next rebuild** — that is the intent, not a
side effect.

## Three things that are unusual here, and why

### The workspace is a bind mount, not a checkout

The job builds `/var/jenkins_home/workspace/resumeforge`, which is this
repository mounted from the host. A local controller pointed at `file:///repo`
would build the last *commit*, which is exactly what you are not testing when you
run a pipeline locally.

`Jenkinsfile` never calls `checkout scm`, so it works both ways: in a real
installation the job is "Pipeline script from SCM" and Jenkins has already
checked out before the first stage runs.

### Docker-outside-of-Docker, and what it costs

The container gets the host's Docker socket and a client, rather than running a
privileged daemon of its own. DinD would need `--privileged` and would rebuild
every layer inside a nested daemon, discarding the host's cache.

The trade is real: **a job that can reach the host's Docker socket can do
anything root can do on the host.** That is acceptable for a local demo of a
pipeline and would not be acceptable on a shared controller. The fix there is an
agent per job — or, if the controller must keep the socket, a socket proxy
(`tecnativa/docker-socket-proxy`) exposing only the endpoints the builds use.

On SELinux hosts the compose file also sets `label=disable`, because the socket
is labelled `container_var_run_t` and a confined container may not open it no
matter which group it is in. The symptom without it is a bare "permission
denied" that looks like a group-id problem and is not.

### Paths: `-v` and `docker build` need *different* ones

This is the detail that costs an afternoon. Under Docker-outside-of-Docker:

* `docker run -v <path>:...` — resolved by the **daemon**, on the host. The
  workspace path does not exist there, so Docker silently creates an empty
  directory and mounts that. The build then fails somewhere else entirely, with
  a missing lockfile or an unwritable cache.
* `docker build <context>` — read by the **client**, inside the container. It
  needs the ordinary workspace path.

So the pipeline uses `$SRC` (the host path, injected as `HOST_WORKSPACE` by
`make ci-up`) for mounts and plain relative paths for build contexts.

## Jenkins vs GitHub Actions, honestly

For *this* workload, GitHub Actions is the better tool, and it is not close:

| | GitHub Actions | Jenkins |
|---|---|---|
| Setup | a YAML file in the repo | an image, a plugin list, a JCasC file, a container to run and patch |
| Runners | ephemeral and free for this repo | this machine, with its own state |
| Service containers | one `services:` block | a network and a container per build, torn down by hand in `post` |
| Path filters | built in | a plugin, or `when { changeset }` |
| Secrets | repository settings | a credential store you administer |
| Upgrades | Microsoft's problem | yours, forever, including plugin compatibility |

Where Jenkins earns its place is everything that is invisible in a small project:
builds that must run inside a network GitHub cannot reach, hardware that cannot
be a hosted runner, an approval workflow tied to an internal identity provider,
or a fleet where one controller serves two hundred repositories and the plugin
set *is* the platform. Those are the conditions that make enterprises run it, and
none of them apply to a portfolio project.

The pipeline is written twice because knowing which tool is better is worth less
than being able to use either, and because "we run Jenkins" is not a question you
get to answer with "we should not".

## What the pipeline does

`Checkout → Services → Build test image → Lint (parallel) → Test (parallel:
ai/api/web) → Build images (parallel) → Smoke test → Push → Deploy`, with
`post { always { junit } }` publishing results even on failure — especially on
failure, which is when someone needs to see which test broke without reading two
thousand lines of console log.

A green run is ~250s and publishes 430 tests. `Push` and `Deploy` are gated on
`branch 'main'` **and** on a configured `REGISTRY` / `DEPLOY_HOST`, so a local
build skips them rather than failing on a registry that does not exist. The
registry credential is bound by id from the credential store — never inlined —
so it can be rotated without touching the pipeline, and the log shows `****`.

## Bugs this pipeline found in the project

Worth recording, because they are the argument for running CI against a clean
environment rather than a developer's machine:

1. **A checkpoint test was passing on stale data.** `test_latest_state_reads_without_advancing`
   used a bare thread id that an earlier fix should have made unique. It passed
   locally only because rows from previous runs were still in the dev database.
   A fresh CI Postgres had nothing to find, and it failed immediately.
2. **`make test-checkpoint` only worked with the dev override applied**, because
   it ran `docker compose exec ai pytest` and the production `ai` image has no
   pytest. It now runs the test image against the compose network.
3. **A flaky readiness check.** `pg_isready` reports "accepting connections"
   during the window when the official Postgres image is still running initdb
   against a temporary server. The next command gets "rejecting connections".
   The pipeline waits on a real `select 1` instead.
