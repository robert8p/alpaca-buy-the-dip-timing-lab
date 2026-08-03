# Changelog

## v2.2.3

- Fixed historical and forward confirmation runs failing with PostgreSQL `IndeterminateDatatype` while updating sealed-window metadata.
- Added explicit PostgreSQL casts for parameters passed into `jsonb_build_object`.
- Added regression coverage for 30-to-90 session window resolution and extension metadata.
- Failed v2.2.2 confirmation runs can be retried after deployment; no schema migration is required.

## 2.2.1

- Fixed the header displaying v2.1.0 while the runtime was v2.2.x.
- The server-rendered UI now reads the package version dynamically from `app.__version__`.
- Clarified that historical and forward confirmation controls appear on a completed parent run page, not the new-run dashboard.

## 2.2.0

- Added a distinct `historical_sealed` run mode.
- Added proper non-overlapping historical 30-session backtests.
- Added unchanged extension from 30 to 90 total historical sessions.
- Retained true-forward 30 → 90 testing as a separate evidence type.
- Added end-to-end and frozen-parent-universe historical scopes.
- Added generic confirmation fields and a v2.2 Supabase migration.
- Added immutable anchor, mode, scope and parent fields to the configuration hash.
- Retained compatibility with v2.1 frozen hashes and routes.
- Added separate historical and forward verdict names.
- Added historical export sealing and UI controls.
- Expanded automated validation to 43 tests.

## 2.1.0

- Added staged true-forward 30 → 90-session testing.
