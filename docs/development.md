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
