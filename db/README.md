# Database Lifecycle

`baseline.sql` is the current schema snapshot for fresh installs and explicit
baseline adoption. Existing installations advance only through forward-managed
migrations; prior enrolled baseline checksums remain recognized when a baseline
snapshot is refreshed alongside a compatibility migration.

`migrations/legacy/` contains historical SQL files preserved as upgrade evidence.
They are never executed automatically.

`migrations/managed/` is the only executable migration directory for future
changes. Managed migration filenames must use a unique, lexically sortable
timestamp prefix:

`YYYYMMDDHHMMSS_domain_description.sql`

Applied managed migrations and the frozen baseline are immutable. If a schema
change is needed, add a new managed migration instead of editing an applied file.
