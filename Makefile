# ResumeForge — developer entry points.
#
# Everything runs in containers. There is no local virtualenv target on purpose:
# the host Python here is 3.14, and pyahocorasick/yake have no wheels for it.
# The container is what makes the build reproducible.

SHELL := /bin/bash
AI_DIR := services/ai
AI_TEST_IMAGE := resumeforge-ai:test
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
test-checkpoint: ## Run checkpointing tests against the running Postgres
	docker compose exec -T \
		-e DATABASE_URL="postgresql://resumeforge:resumeforge@postgres:5432/resumeforge" \
		ai pytest tests/test_checkpointing.py -v

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

# ── Jenkins (Part 18) ─────────────────────────────────────────────────────
# HOST_WORKSPACE is the repository path as the *host's* Docker daemon sees it.
# The Jenkins container mounts this tree as its workspace, but the daemon it
# drives is the host's, so every `-v` inside a build resolves against the host
# filesystem. Without this the builds mount empty directories.
JENKINS_COMPOSE = HOST_WORKSPACE="$(PWD)" docker compose --env-file .env -f infra/jenkins/docker-compose.yml
.PHONY: demo
demo: ## SPENDS ~2 GEMINI CALLS. Full pipeline on the real resume.
	@mkdir -p out && chmod 777 out
	docker run --rm --env-file .env \
		-v "$(PWD)/$(AI_DIR)":/app:z -v "$(PWD)/out":/out:z \
		resumeforge-ai:runtime-test python demo/e2e.py
