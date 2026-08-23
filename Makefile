# ResumeForge — developer entry points.
#
# Everything runs in containers. There is no local virtualenv target on purpose:
# the host Python here is 3.14, and pyahocorasick/yake have no wheels for it.
# The container is what makes the build reproducible.

SHELL := /bin/bash
AI_DIR := services/ai
AI_TEST_IMAGE := resumeforge-ai:test
API_DIR := services/api
# :z relabels the mount for SELinux (Fedora). Without it the container gets
# PermissionError on every file under /app.
AI_RUN := docker run --rm -v "$(PWD)/$(AI_DIR)":/app:z $(AI_TEST_IMAGE)

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ── AI service ────────────────────────────────────────────────────────────
.PHONY: build-test
build-test: ## Build the AI test image (no Chromium/TeX Live, so it is fast)
	docker build --target test -t $(AI_TEST_IMAGE) $(AI_DIR)

.PHONY: build
build: ## Build the AI runtime image (adds Chromium + TeX Live, ~1.5GB)
	docker build --target runtime -t resumeforge-ai:latest $(AI_DIR)

.PHONY: test
test: ## Run the test suite
	$(AI_RUN) pytest tests/ -q

.PHONY: test-v
test-v: ## Run the test suite, verbose
	$(AI_RUN) pytest tests/ -v

.PHONY: bench
bench: ## Print the Aho-Corasick benchmark tables
	$(AI_RUN) pytest tests/test_benchmark.py -s --no-header

.PHONY: integration
integration: ## Run the suite with .env loaded. Live LLM tests still SKIP by default.
	docker run --rm --env-file .env -v "$(PWD)/$(AI_DIR)":/app:z $(AI_TEST_IMAGE) pytest tests/ -v

.PHONY: test-live
test-live: ## SPENDS GEMINI QUOTA (free tier: 20/day, 5/min). Opt-in only.
	@echo "This spends real API quota. Free tier allows 20 requests/day."
	@read -p "Continue? [y/N] " ok && [ "$$ok" = "y" ] || exit 1
	docker run --rm --env-file .env -e RUN_LIVE_LLM_TESTS=1 \
		-v "$(PWD)/$(AI_DIR)":/app:z $(AI_TEST_IMAGE) pytest tests/ -v -m live

.PHONY: build-runtime-test
build-runtime-test: ## Build the runtime image plus test tooling
	docker build --target runtime-test -t resumeforge-ai:runtime-test $(AI_DIR)

.PHONY: test-runtime
test-runtime: build-runtime-test ## Verify Chromium + pdflatex work as the service user
	docker run --rm resumeforge-ai:runtime-test pytest tests/test_runtime_image.py -v

.PHONY: test-checkpoint
# Runs the test image against the compose Postgres over its network, rather than
# `docker compose exec ai`: the `ai` container runs the *runtime* image in
# production mode and has no pytest, so the exec form only worked when the dev
# override happened to be applied.
test-checkpoint: ## Run checkpointing tests against the running Postgres
	docker run --rm --network resumeforge_backend \
		-e DATABASE_URL="postgresql://resumeforge:resumeforge@postgres:5432/resumeforge" \
		-v "$(PWD)/$(AI_DIR)":/app:z -w /app $(AI_TEST_IMAGE) \
		pytest tests/test_checkpointing.py -v

.PHONY: lint
lint: ## Run the same gates CI runs
	$(AI_RUN) ruff check .
	$(AI_RUN) ruff format --check .

.PHONY: fmt
fmt: ## Auto-format and auto-fix
	$(AI_RUN) ruff check --fix .
	$(AI_RUN) ruff format .

.PHONY: graph
graph: ## Print the pipeline graph as Mermaid
	$(AI_RUN) python -c "from app.graph.builder import graph_mermaid; print(graph_mermaid())"

# ── Full stack ────────────────────────────────────────────────────────────
.PHONY: up
up: ## Start the stack (dev mode: hot reload, no Chromium/TeX)
	docker compose up -d
	@$(MAKE) --no-print-directory wait

.PHONY: up-prod
up-prod: ## Start the stack as production does, without the dev override
	docker compose -f docker-compose.yml up -d --build
	@$(MAKE) --no-print-directory wait

.PHONY: wait
wait: ## Block until every service reports healthy
	@echo "waiting for services..."
	@for i in $$(seq 1 60); do \
		unhealthy=$$(docker compose ps --format '{{.Service}} {{.Health}}' \
			| awk '$$2 != "healthy" && $$2 != "" {print $$1}'); \
		if [ -z "$$unhealthy" ]; then echo "all healthy"; exit 0; fi; \
		sleep 2; \
	done; \
	echo "timed out; current state:"; docker compose ps; exit 1

.PHONY: down
down: ## Stop the stack, keeping data
	docker compose down

.PHONY: clean
clean: ## Stop the stack and delete volumes
	docker compose down -v

.PHONY: ps
ps: ## Show service health
	docker compose ps

.PHONY: logs
logs: ## Follow all logs
	docker compose logs -f

.PHONY: logs-ai
logs-ai: ## Follow AI service logs
	docker compose logs -f ai

.PHONY: psql
psql: ## Open a psql shell on the running database
	docker compose exec postgres psql -U resumeforge -d resumeforge

.PHONY: smoke
smoke: ## Hit the running stack the way CI does
	@set -euo pipefail; \
	source .env; \
	echo "-- /health"; curl -fsS localhost:8000/health; echo; \
	echo "-- /ready";  curl -fsS localhost:8000/ready;  echo; \
	echo "-- /internal/extract without a key (expect 401)"; \
	code=$$(curl -s -o /dev/null -w '%{http_code}' -X POST \
		-H 'Content-Type: application/json' -d '{"job_text":"x"}' \
		localhost:8000/internal/extract); \
	test "$$code" = "401" && echo "401 ✓" || { echo "expected 401, got $$code"; exit 1; }; \
	echo "-- /internal/extract with a key"; \
	curl -fsS -X POST -H "x-internal-key: $$INTERNAL_API_KEY" \
		-H 'Content-Type: application/json' \
		-d "$$(python3 -c 'import json,pathlib; print(json.dumps({"job_text": pathlib.Path("services/ai/tests/fixtures/sample_jd.txt").read_text(), "max_keywords": 8}))')" \
		localhost:8000/internal/extract \
		| python3 -m json.tool | head -40

.PHONY: smoke-pipeline
smoke-pipeline: ## Start a session over HTTP and prove the interrupt is durable. Spends no tokens.
	@set -euo pipefail; \
	source .env; \
	sid="smoke-$$(date +%s)"; \
	echo "-- POST /internal/pipeline/run ($$sid)"; \
	python3 -c 'import json,pathlib,sys; print(json.dumps({"session_id": sys.argv[1], "user_id": "smoke", "user_latex": pathlib.Path("services/ai/tests/fixtures/real_resume.tex").read_text(), "user_profile": json.load(open("services/ai/tests/fixtures/real_profile.json")), "job_text": pathlib.Path("services/ai/tests/fixtures/sample_jd.txt").read_text()}))' "$$sid" > /tmp/rf-run.json; \
	curl -fsS -X POST -H "x-internal-key: $$INTERNAL_API_KEY" -H 'Content-Type: application/json' \
		-d @/tmp/rf-run.json localhost:8000/internal/pipeline/run; echo; \
	sleep 2; \
	echo "-- GET events (SSE, closes at the keyword gate)"; \
	curl -fsS -N -m 10 -H "x-internal-key: $$INTERNAL_API_KEY" \
		localhost:8000/internal/pipeline/$$sid/events | head -8; \
	echo "-- restarting the ai container mid-session"; \
	docker compose restart ai >/dev/null; \
	until curl -fsS localhost:8000/health >/dev/null 2>&1; do sleep 1; done; \
	echo "-- GET status (a different process, same paused session)"; \
	curl -fsS -H "x-internal-key: $$INTERNAL_API_KEY" localhost:8000/internal/pipeline/$$sid \
		| python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["paused_at"]=="keyword_review", d; print("still paused at", d["paused_at"], "with", len(d["keyword_review"]["keywords"]), "keywords ✓")'

# ── The API service's database (Part 12) ──────────────────────────────────
# Node runs in a container like everything else, attached to the compose network
# so `postgres` resolves, and as the host user so nothing it writes ends up
# root-owned. NODE_RUN is one line rather than five copies of it.
NODE_IMAGE := node:22
# --env-file supplies the shared secrets; the explicit -e after it overrides
# DATABASE_URL, because the API service reaches the database through its own
# `app` schema and .env's copy points at `public` (where LangGraph lives).
NODE_RUN = docker run --rm -u "$$(id -u):$$(id -g)" -e HOME=/tmp \
	--network resumeforge_backend --env-file .env \
	-e DATABASE_URL="$$API_DATABASE_URL" \
	-v "$(PWD)":/repo:z -w /repo/$(API_DIR) $(NODE_IMAGE)

.PHONY: api-install
api-install: ## Install the API service's dependencies
	@set -a; source .env; set +a; $(NODE_RUN) npm install --no-audit --no-fund

.PHONY: db-migrate
db-migrate: ## Apply Prisma migrations to the `app` schema
	@set -a; source .env; set +a; $(NODE_RUN) sh -c "npx prisma migrate deploy && npx prisma generate"

.PHONY: db-seed
db-seed: ## Seed the real profile. Idempotent.
	@set -a; source .env; set +a; $(NODE_RUN) npx tsx prisma/seed.ts

.PHONY: db-diff
# Diffs the *live* database against schema.prisma. `--from-migrations` would be
# the more orthodox source, but it needs a shadow database to replay into, and
# the obvious shadow URL is the real one -- which Prisma drops and recreates.
# `--from-url` only reads.
db-diff: ## Write a new migration from the current schema.prisma. Never `migrate dev`.
	@test -n "$(NAME)" || { echo "usage: make db-diff NAME=add_something"; exit 1; }
	@set -a; source .env; set +a; \
	mkdir -p $(API_DIR)/prisma/migrations/$(NAME); \
	$(NODE_RUN) npx prisma migrate diff \
		--from-url "$$API_DATABASE_URL" --to-schema-datamodel ./prisma/schema.prisma --script \
		> $(API_DIR)/prisma/migrations/$(NAME)/migration.sql
	@echo "wrote $(API_DIR)/prisma/migrations/$(NAME)/migration.sql -- review it before applying"

.PHONY: api-test
api-test: ## Typecheck and test the API service
	@set -a; source .env; set +a; $(NODE_RUN) sh -c "npx tsc --noEmit && npx vitest run"

.PHONY: db-psql-app
db-psql-app: ## psql with the search path set to the API's schema
	docker compose exec postgres psql -U resumeforge -d resumeforge -c "set search_path to app" -P pager=off

.PHONY: smoke-gateway
smoke-gateway: ## Drive the flow through the gateway only. Spends no tokens.
	@set -euo pipefail; \
	echo "-- the AI service must NOT be reachable from the host"; \
	code=$$(curl -s -m 3 -o /dev/null -w '%{http_code}' localhost:8000/health || true); \
	test "$$code" = "000" && echo "localhost:8000 unreachable ✓" || \
		{ echo "AI service answered $$code from the host; it should be network-internal"; \
		  echo "(the dev override publishes it on purpose -- run with -f docker-compose.yml)"; }; \
	echo "-- /health and /ready"; curl -fsS localhost:4000/health; echo; curl -fsS localhost:4000/ready; echo; \
	echo "-- unauthenticated (expect 401)"; \
	code=$$(curl -s -o /dev/null -w '%{http_code}' localhost:4000/api/profile); \
	test "$$code" = "401" && echo "401 ✓" || { echo "expected 401, got $$code"; exit 1; }; \
	echo "-- dev token"; \
	token=$$(curl -fsS -X POST localhost:4000/api/auth/dev-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])'); \
	echo "-- the profile the pipeline will be given"; \
	curl -fsS -H "Authorization: Bearer $$token" localhost:4000/api/profile \
		| python3 -c 'import json,sys; d=json.load(sys.stdin); p=d["profile"]; print(" experiences:", [e["company"] for e in p["experiences"]]); print(" projects   :", [x["name"] for x in p["projects"]]); print(" skills:", len(p["skills"]), "achievements:", len(p["achievements"]), "latex:", d["hasLatexTemplate"])'; \
	echo "-- start a session"; \
	body=$$(python3 -c 'import json,pathlib; print(json.dumps({"jobText": pathlib.Path("services/ai/tests/fixtures/sample_jd.txt").read_text()}))'); \
	sid=$$(curl -fsS -X POST -H "Authorization: Bearer $$token" -H 'Content-Type: application/json' \
		-d "$$body" localhost:4000/api/sessions | python3 -c 'import json,sys; print(json.load(sys.stdin)["sessionId"])'); \
	echo "   session $$sid"; \
	echo "-- SSE, relayed through the gateway"; \
	curl -fsS -N -m 8 -H "Authorization: Bearer $$token" localhost:4000/api/sessions/$$sid/stream | head -6; \
	echo "-- status at the keyword gate"; \
	curl -fsS -H "Authorization: Bearer $$token" localhost:4000/api/sessions/$$sid \
		| python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["paused_at"]=="keyword_review", d; print(" paused at", d["paused_at"], "with", len(d["keyword_review"]["keywords"]), "keywords ✓")'; \
	echo; echo "Everything past this gate spends Gemini quota. To continue:"; \
	echo "  curl -X POST -H \"Authorization: Bearer \$$token\" localhost:4000/api/sessions/$$sid/keywords -d '{}' -H 'Content-Type: application/json'"

.PHONY: github-sync
github-sync: ## Sync projects from your real GitHub. Needs GITHUB_TOKEN in .env.
	@set -euo pipefail; \
	source .env; \
	test -n "$$GITHUB_TOKEN" || { \
		echo "GITHUB_TOKEN is empty in .env."; \
		echo "Create a classic PAT with the 'repo' scope (read is enough) and set it there."; \
		exit 1; }; \
	token=$$(curl -fsS -X POST localhost:4000/api/auth/dev-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])'); \
	echo "-- storing the PAT (encrypted at rest)"; \
	curl -fsS -X PUT -H "Authorization: Bearer $$token" -H 'Content-Type: application/json' \
		-d "$$(python3 -c 'import json,os; print(json.dumps({"token": os.environ["GITHUB_TOKEN"], "username": os.environ.get("GITHUB_USERNAME") or None}))')" \
		localhost:4000/api/profile/github/token; echo; \
	echo "-- first sync"; \
	curl -fsS -X POST -H "Authorization: Bearer $$token" localhost:4000/api/profile/github/sync \
		| python3 -m json.tool; \
	echo "-- second sync, immediately (must make zero API calls)"; \
	curl -fsS -X POST -H "Authorization: Bearer $$token" localhost:4000/api/profile/github/sync \
		| python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="fresh" and d["apiRequests"]==0, d; print(" status=fresh, apiRequests=0 ✓")'; \
	echo "-- forced sync (asks GitHub, expects a free 304)"; \
	curl -fsS -X POST -H "Authorization: Bearer $$token" "localhost:4000/api/profile/github/sync?force=true" \
		| python3 -m json.tool; \
	echo "-- the projects the pipeline can now draw on"; \
	curl -fsS -H "Authorization: Bearer $$token" localhost:4000/api/profile \
		| python3 -c 'import json,sys; p=json.load(sys.stdin)["profile"]; [print("  ", x["name"], "--", ", ".join(x["tech"][:4])) for x in p["projects"]]'

.PHONY: e2e
e2e: ## Drive the browser journey. Free by default; FULL=1 spends Gemini quota.
	@mkdir -p out && chmod 777 out
	@# --network host so `localhost:3000` and `localhost:4000` mean the same
	@# thing inside the container as they do in a real browser on this machine:
	@# the client bundle is built with a localhost API URL, so a bridge network
	@# would make every fetch from the page fail.
	docker run --rm --network host \
		-e WEB_URL=http://localhost:$${WEB_PUBLISH_PORT:-3000} -e FULL="$(FULL)" \
		-e SESSION_URL="$(SESSION_URL)" \
		-v "$(PWD)/$(AI_DIR)":/app:z \
		-v "$(PWD)/services/web/e2e":/e2e:z \
		-v "$(PWD)/out":/out:z \
		resumeforge-ai:runtime-test python /e2e/journey.py

# ── Jenkins (Part 18) ─────────────────────────────────────────────────────
# HOST_WORKSPACE is the repository path as the *host's* Docker daemon sees it.
# The Jenkins container mounts this tree as its workspace, but the daemon it
# drives is the host's, so every `-v` inside a build resolves against the host
# filesystem. Without this the builds mount empty directories.
JENKINS_COMPOSE = HOST_WORKSPACE="$(PWD)" docker compose --env-file .env -f infra/jenkins/docker-compose.yml

.PHONY: ci-up
ci-up: ## Start Jenkins (http://localhost:8080, admin/admin by default)
	$(JENKINS_COMPOSE) up -d --build
	@echo "Jenkins starting at http://localhost:$${JENKINS_PORT:-8080}"

.PHONY: ci-down
ci-down: ## Stop Jenkins, keeping its volume
	$(JENKINS_COMPOSE) down

.PHONY: ci-reset
ci-reset: ## Stop Jenkins and delete its volume, so the next start reconfigures from casc.yaml
	$(JENKINS_COMPOSE) down -v

.PHONY: ci-logs
ci-logs: ## Follow the Jenkins controller log
	$(JENKINS_COMPOSE) logs -f jenkins

.PHONY: ci-build
ci-build: ## Trigger a Jenkins build and wait for the result
	@set -a; source .env; set +a; ./infra/jenkins/build.sh

# ── Kubernetes (Part 19) ──────────────────────────────────────────────────
KIND_CLUSTER := resumeforge
K8S_NS := resumeforge
# The nodes that receive side-loaded images. Both workers by default; the AI
# image is 3GB (Chromium + TeX Live), so on a machine short of disk this can be
# narrowed to one and the dev overlay's nodeSelector will keep pods there.
K8S_IMAGE_NODES ?= $(KIND_CLUSTER)-worker,$(KIND_CLUSTER)-worker2
# ingress-nginx's kind manifest, pinned: the chart's defaults move, and one of
# those moves is what silently unhooked the controller from the host ports.
INGRESS_MANIFEST := https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.13.0/deploy/static/provider/kind/deploy.yaml

.PHONY: k8s-up
k8s-up: ## Create the cluster, load images, apply the dev overlay, migrate and seed
	kind create cluster --config infra/k8s/kind-cluster.yaml
	kubectl apply -f $(INGRESS_MANIFEST)
	kubectl -n ingress-nginx patch deploy ingress-nginx-controller \
		--patch-file infra/k8s/ingress-nginx-kind-patch.yaml
	kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=300s
	$(MAKE) k8s-images
	$(MAKE) k8s-apply
	@echo "ResumeForge is at http://localhost:8081"

.PHONY: k8s-images
k8s-images: ## Build the cluster images and side-load them onto the workers
	@# The browser reaches the gateway through the ingress, and NEXT_PUBLIC_* is
	@# baked in at build time, so the web image is rebuilt for that origin.
	docker build --target runtime -t resumeforge-ai:k8s $(AI_DIR)
	docker build -t resumeforge-api:k8s $(API_DIR)
	docker build --build-arg NEXT_PUBLIC_API_URL=http://localhost:8081 -t resumeforge-web:k8s services/web
	@# Workers only: the control plane is tainted and runs no application pods,
	@# so a copy there is several gigabytes of nothing.
	for img in resumeforge-ai:k8s resumeforge-api:k8s resumeforge-web:k8s; do \
		kind load docker-image $$img --name $(KIND_CLUSTER) \
			--nodes $(K8S_IMAGE_NODES); \
	done
	@# Mark the nodes that now hold the images. The dev overlay pins application
	@# pods to this label, because a pod scheduled onto a node without a copy
	@# fails ImagePullBackOff -- there is no registry to pull from.
	kubectl label node $$(echo $(K8S_IMAGE_NODES) | tr ',' ' ') \
		resumeforge.dev/images-loaded=true --overwrite

.PHONY: k8s-apply
k8s-apply: ## Apply the dev overlay and run migrations
	kubectl apply -k infra/k8s/overlays/dev
	@# The seed reads the pipeline's own fixtures; they are injected rather than
	@# baked into the image, which contains only services/api.
	kubectl -n $(K8S_NS) create configmap seed-data \
		--from-file=$(AI_DIR)/tests/fixtures/real_profile.json \
		--from-file=$(AI_DIR)/tests/fixtures/real_resume.tex \
		--dry-run=client -o yaml | kubectl apply -f -
	@# Job specs are immutable, so re-applying needs the old one gone first.
	kubectl -n $(K8S_NS) delete job db-migrate --ignore-not-found
	kubectl apply -k infra/k8s/overlays/dev
	kubectl -n $(K8S_NS) wait --for=condition=complete job/db-migrate --timeout=300s
	kubectl -n $(K8S_NS) rollout status deploy/api --timeout=300s

.PHONY: k8s-status
k8s-status: ## Pods, services, ingress and the autoscaler
	kubectl -n $(K8S_NS) get pods -o wide
	kubectl -n $(K8S_NS) get svc,ingress,hpa

.PHONY: k8s-demo
k8s-demo: ## Start a run, destroy every AI pod, show the session survived. Spends nothing.
	@set -euo pipefail; \
	base=http://localhost:8081; \
	token=$$(curl -fsS -X POST $$base/api/auth/dev-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])'); \
	body=$$(python3 -c 'import json,pathlib; print(json.dumps({"jobText": pathlib.Path("$(AI_DIR)/tests/fixtures/sample_jd.txt").read_text()}))'); \
	sid=$$(curl -fsS -X POST -H "Authorization: Bearer $$token" -H 'Content-Type: application/json' \
		-d "$$body" $$base/api/sessions | python3 -c 'import json,sys; print(json.load(sys.stdin)["sessionId"])'); \
	echo "session $$sid started"; sleep 6; \
	curl -fsS -H "Authorization: Bearer $$token" $$base/api/sessions/$$sid \
		| python3 -c 'import json,sys; d=json.load(sys.stdin); print(" paused at", d["paused_at"], "with", len(d["keyword_review"]["keywords"]), "keywords")'; \
	echo "-- destroying every AI pod --"; \
	kubectl -n $(K8S_NS) get pods -l app=ai --no-headers | awk '{print "   " $$1}'; \
	kubectl -n $(K8S_NS) delete pod -l app=ai --wait=false >/dev/null; \
	sleep 5; kubectl -n $(K8S_NS) rollout status deploy/ai --timeout=300s; \
	kubectl -n $(K8S_NS) get pods -l app=ai --no-headers | awk '{print "   " $$1, $$5}'; \
	echo "-- the same session, on pods that did not exist when it started --"; \
	curl -fsS -H "Authorization: Bearer $$token" $$base/api/sessions/$$sid \
		| python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["paused_at"]=="keyword_review", d; print(" paused at", d["paused_at"], "with", len(d["keyword_review"]["keywords"]), "keywords ✓")'; \
	echo "-- and it continues, not just reads --"; \
	curl -fsS -X POST -H "Authorization: Bearer $$token" -H 'Content-Type: application/json' -d '{}' \
		$$base/api/sessions/$$sid/keywords >/dev/null; \
	for i in $$(seq 1 30); do sleep 6; \
		step=$$(curl -fsS -H "Authorization: Bearer $$token" $$base/api/sessions/$$sid \
			| python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["step"], d["paused_at"])'); \
		echo "   $$step"; \
		case "$$step" in *human_review*|*FAILED*) break;; esac; \
	done

.PHONY: k8s-logs
k8s-logs: ## Follow the AI service's logs across all replicas
	kubectl -n $(K8S_NS) logs -f -l app=ai --max-log-requests=6 --tail=20

.PHONY: k8s-down
k8s-down: ## Delete the cluster
	kind delete cluster --name $(KIND_CLUSTER)

# ── Observability (Part 20) ───────────────────────────────────────────────
OBS_COMPOSE = docker compose -f docker-compose.yml -f docker-compose.observability.yml

.PHONY: observability
observability: ## Start the stack with Prometheus and Grafana
	$(OBS_COMPOSE) up -d --build
	@echo "Grafana    http://localhost:$${GRAFANA_PUBLISH_PORT:-3001}  (admin/admin)"
	@echo "Prometheus http://localhost:$${PROMETHEUS_PUBLISH_PORT:-9090}"

.PHONY: observability-down
observability-down: ## Stop Prometheus and Grafana, leave the app running
	$(OBS_COMPOSE) stop prometheus grafana

.PHONY: metrics
metrics: ## Print the interesting metric lines from both services
	@set -euo pipefail; \
	echo "── AI service ──"; \
	docker compose exec -T ai python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/metrics').read().decode())" \
		| grep -E "^resumeforge_(pipeline_runs|llm_tokens|guardrail|extraction_duration_seconds_count|pdf_compilations)" | head -20; \
	echo "── Gateway ──"; \
	curl -fsS localhost:$${API_PUBLISH_PORT:-4000}/metrics \
		| grep -E "^resumeforge_api_(auth_failures|rate_limited|event_streams)" | head -10

.PHONY: demo
demo: ## SPENDS ~2 GEMINI CALLS. Full pipeline on the real resume.
	@mkdir -p out && chmod 777 out
	docker run --rm --env-file .env \
		-v "$(PWD)/$(AI_DIR)":/app:z -v "$(PWD)/out":/out:z \
		resumeforge-ai:runtime-test python demo/e2e.py
