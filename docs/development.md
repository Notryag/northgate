# Development workflow

Status: accepted  
Last reviewed: 2026-07-17

## Verification cadence

Development should keep the main implementation flow moving and use concentrated
verification instead of rerunning the complete test suite after every edit.

- During a feature slice, run only checks needed to resolve a concrete risk or failure.
- Run the relevant focused checks when a contract, migration, security boundary, or
  streaming behavior changes.
- Run the complete backend and frontend quality checks once at a meaningful commit,
  merge, milestone, or release boundary.
- Record manual integration or recovery verification in the owning roadmap or
  operations page when it establishes an exit criterion.
- CI remains the independent full-suite gate for every push and pull request.

This cadence changes when tests run, not the quality bar. A feature slice is not
complete while a known failure remains or its critical path has not been exercised.

## Production verification log

### 2026-07-17: Dayboard single-tenant canary

- Built the Dayboard API/worker image from clean commit `b6c0f58`; uncommitted
  workspace changes were excluded from the image.
- Deployed the image to API and worker while leaving the web service and the
  original provider connection unchanged.
- Enabled Northgate only for the newer of two production tenants. Each tenant had
  one member at the time of deployment, so the allowlist represented one user.
- Confirmed both containers were healthy, could resolve `northgate` on the shared
  `platform-infra` network, and received HTTP 200 from Northgate readiness.
- Exercised Dayboard's deployed selection logic without another model request:
  the canary tenant selected the Northgate connection and trusted metadata, while
  the control tenant selected the original connection without Northgate metadata.
- Confirmed the Northgate and Dayboard startup logs contained no errors after the
  deployment.

The pre-canary Dayboard environment backup is stored at
`/var/backups/dayboard/config/dayboard-env-pre-canary-20260717T151852Z.env` with a
root-only checksum. Rollback restores that file and recreates only the Dayboard API
and worker containers. Provider and application secrets are stored separately as
root-only files and are intentionally not recorded here.

### 2026-07-18: Dayboard full tenant rollout

- Observed the single-tenant deployment for 11 hours. Northgate, Dayboard API, and
  Dayboard worker remained healthy, and the previous Northgate documentation CI
  run completed successfully.
- No organic canary traffic arrived during that window, so ran one minimal
  Dayboard agent smoke through the deployed factory, Northgate, and the real
  provider without writing Dayboard business data or printing model content.
- The smoke completed with HTTP 200 and outcome `succeeded`: one provider attempt,
  no retry or fallback, 1,169 ms total latency, 1,164 ms first-token latency, and
  complete tenant, user, and run attribution.
- Added the remaining production tenant to the allowlist. Production contained two
  tenants with one member each at rollout time; both now select Northgate and
  trusted metadata through Dayboard's deployed configuration.
- Recreated only the API and worker containers, confirmed both healthy, confirmed
  HTTP 200 from Northgate readiness, and found no startup or proxy errors.
- Kept Dayboard's original provider connection in the environment for rollback.

The pre-full-rollout environment backup is stored at
`/var/backups/dayboard/config/dayboard-env-pre-full-northgate-20260718T023133Z.env`
with a verified root-only checksum. Restoring it and recreating only API and worker
returns Dayboard to the single-tenant canary.

### 2026-07-18: Tenant usage analytics

- Added operator-only tenant aggregates and the React console tenant table in
  commit `7fc90cb`.
- Ran Ruff lint and format checks for the affected backend files and the focused
  tenant analytics test; the test passed, including operator authorization and
  omission of user/run metadata.
- Ran the console TypeScript check and production build successfully. No browser
  screenshots were created.
- Executed the SQLAlchemy tenant aggregation against the production PostgreSQL
  schema before deployment; all 19 historical records were classified as
  succeeded with no in-flight or error records.
- Rebuilt and replaced only the Northgate application container. The deployed
  endpoint rejected an unauthenticated request with HTTP 401 and returned, for its
  default 24-hour range, six successful requests across three groups, including two
  attributed tenant groups, with no errors or in-flight records.
- Confirmed the console returned HTTP 200, its bundle contained the tenant view,
  and Dayboard still received HTTP 200 from Northgate readiness over the shared
  platform network.
