# Managed Migrations

Place all future executable schema changes in this directory.

Rules:

- Do not place historical backfill files here.
- Do not edit an applied migration file.
- Do not edit `db/baseline.sql`; add a new managed migration instead.
- Use filenames shaped like `YYYYMMDDHHMMSS_domain_description.sql`.

`db/migrations/legacy/` remains historical evidence only and is never replayed
automatically by the migration runner.
