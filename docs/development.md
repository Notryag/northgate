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
