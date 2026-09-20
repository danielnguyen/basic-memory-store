# Managed Migrations

Place all future executable schema changes in this directory.

Rules:

- Do not place historical backfill files here.
- Do not edit an applied migration file.
- Pair every schema change with a managed migration. When clean-install parity
  requires refreshing `db/baseline.sql`, preserve recognition of the prior
  enrolled baseline checksum in the migration runner.
- Use filenames shaped like `YYYYMMDDHHMMSS_domain_description.sql`.

`db/migrations/legacy/` remains historical evidence only and is never replayed
automatically by the migration runner.

`20260920120000_work_items.sql` adds bounded work lifecycle records and an
explicit owner/client current-work locator. The refreshed baseline contains the
same schema; the runner recognizes the previous enrolled baseline checksum.
