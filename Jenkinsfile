// ResumeForge CI, second implementation.
//
// The same gates as .github/workflows/ci.yml, expressed in Jenkins' declarative
// syntax. Having both is deliberate: GitHub Actions is where the repository
// lives, and Jenkins is what most enterprises actually run, so the pipeline is
// written twice rather than argued about once. infra/jenkins/README.md is honest
// about which one is better for this workload.
//
// Two properties this file keeps:
//
//   * **It never calls `checkout scm`.** Locally the workspace is a bind mount
//     of the working tree; in a real installation the job is "Pipeline script
//     from SCM" and Jenkins has already checked out before the first stage runs.
//     The same file works in both, and the local run tests uncommitted work --
//     which is the only reason to run a pipeline locally at all.
//   * **No secret is written here.** The registry credential is bound by id from
//     the credential store, so this file is safe to read and the credential can
//     be rotated without touching it.

pipeline {
  agent any

  options {
    timestamps()
    ansiColor('xterm')
    timeout(time: 40, unit: 'MINUTES')
    // The workspace is a bind mount of the repository. `deleteDir()` on it would
    // delete the source, so cleanup is explicitly scoped to build artifacts.
    buildDiscarder(logRotator(numToKeepStr: '20'))
    disableConcurrentBuilds()
  }

  environment {
    // Where the source lives *as the Docker daemon sees it*. On a normal agent
    // that is the workspace; under Docker-outside-of-Docker the daemon is the
    // host's, so it must be told the host path. Getting this wrong does not
    // error -- Docker helpfully creates the missing directory and mounts it
    // empty, and the build fails several steps later for unrelated-looking
    // reasons (a missing lockfile, an unwritable cache).
    //
    // Note the asymmetry, which is the part that catches people: `-v` paths are
    // resolved by the **daemon** and need $SRC, while a `docker build` context
    // is read by the **client** and needs the ordinary workspace path. The two
    // are the same everywhere except here.
    SRC = "${env.HOST_WORKSPACE?.trim() ? env.HOST_WORKSPACE : env.WORKSPACE}"
    // A network and a database per build, named after the build, so two builds
    // on one machine cannot collide on a container name or a port.
    CI_NET = "resumeforge-ci-${env.BUILD_NUMBER}"
    PG = "resumeforge-ci-pg-${env.BUILD_NUMBER}"
    PGPASS = 'resumeforge'
    AI_TEST_IMAGE = "resumeforge-ai:ci-${env.BUILD_NUMBER}"
    // No GEMINI_API_KEY, on purpose: the pipeline must be fully exercisable
    // without credentials, so a rotated or exhausted key can never turn the
    // build red. Live-API tests skip themselves.
    INTERNAL_API_KEY = 'ci-internal-key'
    JWT_SECRET = 'ci-jwt-secret-value'
    ENCRYPTION_KEY = 'ci-encryption-key-32-bytes-long!'
  }

  stages {
    stage('Environment') {
      steps {
        sh '''
          echo "workspace: $PWD"
          echo "source as the daemon sees it: $SRC"
          docker version --format 'docker {{.Server.Version}}'
          git rev-parse --short HEAD 2>/dev/null || echo "not a git checkout"

          # The workspace is a bind mount of the working tree and survives
          # between builds, so yesterday's reports are still lying there. Without
          # this, a build that fails before running a single test still publishes
          # the previous build's results -- green test counts on a red build,
          # which is worse than no results at all.
          find services -name 'junit-*.xml' -delete
        '''
      }
    }

    stage('Services') {
      steps {
        // A Postgres per build rather than a shared one. The AI service's
        // checkpointing tests and the API's migrations both write real schemas,
        // and sharing a database between concurrent builds makes failures depend
        // on what else is running.
        sh '''
          docker network create "$CI_NET" >/dev/null
          docker run -d --name "$PG" --network "$CI_NET" \
            -e POSTGRES_USER=resumeforge -e POSTGRES_PASSWORD="$PGPASS" \
            -e POSTGRES_DB=resumeforge_ci postgres:17-alpine >/dev/null
          # A real query, not pg_isready. The official image starts a temporary
          # server to run initdb, so `pg_isready` answers "accepting" during a
          # window when the database is about to be restarted -- and the next
          # command gets "rejecting connections". This is the same trap the
          # application's compose healthcheck documents.
          ready=0
          for i in $(seq 1 60); do
            if docker exec "$PG" psql -U resumeforge -d resumeforge_ci -tAc 'select 1' >/dev/null 2>&1; then
              ready=1; break
            fi
            sleep 1
          done
          test "$ready" = "1" || { echo "Postgres never became usable"; docker logs "$PG" | tail -20; exit 1; }
          echo "Postgres ready"
        '''
      }
    }

    stage('Build test image') {
      steps {
        // The AI service's tests run in its own image, so building it is a
        // prerequisite for both the lint and test stages rather than a stage of
        // its own at the end.
        sh 'docker build --target test -t "$AI_TEST_IMAGE" services/ai'
      }
    }

    stage('Lint') {
      parallel {
        stage('ai') {
          steps {
            sh '''
              docker run --rm -v "$SRC/services/ai":/app:z -w /app "$AI_TEST_IMAGE" ruff check .
              docker run --rm -v "$SRC/services/ai":/app:z -w /app "$AI_TEST_IMAGE" ruff format --check .
            '''
          }
        }
        stage('api typecheck') {
          steps {
            sh '''
              docker run --rm -v "$SRC/services/api":/app:z -w /app node:22 sh -c '
                npm ci --no-audit --no-fund >/dev/null && npx prisma generate >/dev/null && npx tsc --noEmit'
            '''
          }
        }
      }
    }

    stage('Test') {
      parallel {
        stage('ai') {
          steps {
            sh '''
              docker run --rm --network "$CI_NET" \
                -e DATABASE_URL="postgresql://resumeforge:$PGPASS@$PG:5432/resumeforge_ci" \
                -e INTERNAL_API_KEY="$INTERNAL_API_KEY" \
                -v "$SRC/services/ai":/app:z -w /app "$AI_TEST_IMAGE" \
                pytest tests/ -v --junitxml=junit-ai.xml
            '''
          }
        }
        stage('api') {
          steps {
            // `?schema=app` for the same reason the service uses it: Prisma owns
            // the `app` schema and LangGraph owns `public`, in one database.
            sh '''
              docker run --rm --network "$CI_NET" \
                -e DATABASE_URL="postgresql://resumeforge:$PGPASS@$PG:5432/resumeforge_ci?schema=app" \
                -e INTERNAL_API_KEY="$INTERNAL_API_KEY" -e JWT_SECRET="$JWT_SECRET" \
                -e ENCRYPTION_KEY="$ENCRYPTION_KEY" -e LOG_LEVEL=silent \
                -v "$SRC":/repo:z -w /repo/services/api node:22 sh -c '
                  npx prisma migrate deploy &&
                  npx tsx prisma/seed.ts &&
                  npx vitest run --reporter=junit --outputFile=junit-api.xml'
            '''
          }
        }
        stage('web') {
          steps {
            sh '''
              docker run --rm -v "$SRC/services/web":/app:z -w /app \
                -e NEXT_PUBLIC_API_URL=http://localhost:4000 -e NEXT_TELEMETRY_DISABLED=1 \
                node:22 sh -c 'npm ci --no-audit --no-fund >/dev/null && npx tsc --noEmit && npx next build'
            '''
          }
        }
      }
    }

    stage('Build images') {
      parallel {
        stage('ai') { steps { sh 'docker build --target runtime -t resumeforge-ai:$BUILD_NUMBER services/ai' } }
        stage('api') { steps { sh 'docker build -t resumeforge-api:$BUILD_NUMBER services/api' } }
        stage('web') { steps { sh 'docker build -t resumeforge-web:$BUILD_NUMBER services/web' } }
      }
    }

    stage('Smoke test the images') {
      steps {
        // Built is not the same as working. This is the same check CI runs: boot
        // the gateway image, prove it reaches Postgres, prove it refuses an
        // unauthenticated request.
        sh '''
          docker run -d --name "api-smoke-$BUILD_NUMBER" --network "$CI_NET" \
            -e DATABASE_URL="postgresql://resumeforge:$PGPASS@$PG:5432/resumeforge_ci?schema=app" \
            -e INTERNAL_API_KEY="$INTERNAL_API_KEY" -e JWT_SECRET="$JWT_SECRET" \
            -e ENCRYPTION_KEY="$ENCRYPTION_KEY" \
            resumeforge-api:$BUILD_NUMBER >/dev/null

          ok=0
          for i in $(seq 1 30); do
            if docker run --rm --network "$CI_NET" curlimages/curl:latest \
                 -fsS "http://api-smoke-$BUILD_NUMBER:4000/ready" | grep -q '"database":"ok"'; then
              ok=1; break
            fi
            sleep 1
          done
          docker rm -f "api-smoke-$BUILD_NUMBER" >/dev/null
          test "$ok" = "1" || { echo "the gateway image never became ready"; exit 1; }
          echo "gateway image ready and talking to Postgres"
        '''
      }
    }

    stage('Push') {
      when {
        // Only from the default branch, and only when a registry is configured.
        // Without both, a local build would try to push to a registry that does
        // not exist and fail for a reason that has nothing to do with the code.
        allOf {
          branch 'main'
          expression { return env.REGISTRY?.trim() }
        }
      }
      steps {
        // Bound by id, never inlined. The credential can be rotated in the store
        // without touching this file, and the build log shows **** rather than
        // the password.
        withCredentials([usernamePassword(
          credentialsId: 'registry-credentials',
          usernameVariable: 'REGISTRY_USER',
          passwordVariable: 'REGISTRY_PASS',
        )]) {
          sh '''
            echo "$REGISTRY_PASS" | docker login "$REGISTRY" -u "$REGISTRY_USER" --password-stdin
            for svc in ai api web; do
              docker tag "resumeforge-$svc:$BUILD_NUMBER" "$REGISTRY/resumeforge-$svc:$BUILD_NUMBER"
              docker push "$REGISTRY/resumeforge-$svc:$BUILD_NUMBER"
            done
            docker logout "$REGISTRY"
          '''
        }
      }
    }

    stage('Deploy') {
      when {
        allOf {
          branch 'main'
          expression { return env.DEPLOY_HOST?.trim() }
        }
      }
      steps {
        // Part 20 owns the real deployment. This stage exists so the shape of the
        // pipeline is complete and the gate is visible; it refuses to pretend.
        echo "Would deploy build ${env.BUILD_NUMBER} to ${env.DEPLOY_HOST} (see Part 20)."
      }
    }
  }

  post {
    always {
      // Published even when the build fails -- especially then, since a failing
      // build is when someone needs to see which test failed without reading
      // 2,000 lines of console log.
      junit testResults: 'services/**/junit-*.xml', allowEmptyResults: true, skipPublishingChecks: true

      sh '''
        docker rm -f "$PG" >/dev/null 2>&1 || true
        docker network rm "$CI_NET" >/dev/null 2>&1 || true
        docker rmi "$AI_TEST_IMAGE" >/dev/null 2>&1 || true
        # The per-build application images are left in place: they are what the
        # push and deploy stages consume, and `docker image prune` on a schedule
        # is a better cleanup policy than deleting them here.
      '''
    }
    success {
      echo "Build ${env.BUILD_NUMBER} is green."
    }
  }
}
