# Changelog

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
