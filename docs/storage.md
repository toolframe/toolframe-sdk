# Portable plugin storage

SDK 0.3 adds explicit PostgreSQL migration variants and generated-row results.
A `Migration.sql` value remains its original SQLite SQL; its checksum is unchanged.
Add `dialects={"postgresql": "..."}` for PostgreSQL. A provider selects a variant
with `migration.for_dialect("postgresql")`, whose checksum covers that selected SQL.
Missing variants are rejected, never translated implicitly. Store revision/parent
ordering stays the same, but each database maintains its own migration history.

Use `storage.execute_returning("INSERT INTO items(label) VALUES (?) RETURNING id",
[label])` for generated IDs. It returns `{"rows": [{"id": 1}], "rowcount": 1}` on
compatible SQLite and PostgreSQL providers. It is also available inside
`async with storage.transaction() as transaction`. Ordinary `execute` retains its
result shape; `lastrowid` is provider-specific and may be null on PostgreSQL.

Methods use positional qmark parameters. Plugins remain responsible for portable
SQL: identity columns, numeric precision, types, functions and DDL can differ.
PostgreSQL support does not imply arbitrary SQLite SQL compatibility. Only use
JSON-compatible result values and follow the provider's documented SQL limits.

Pass stable `mutation_id` values when acknowledgement loss must be reconciled.
Use `storage.receipt(mutation_id)` before retrying an uncertain write. A transaction
that already committed reports `already_committed` when begun with that ID; its
body must not run twice. Transaction tokens are scoped to an admitted root and
worker generation and are not durable recovery handles.

Providers that connect to external state must declare
`runtime_validation="separate_application"` on their `PluginDefinition`. A
scratch application home cannot isolate inherited database credentials. Core
refuses its optional scratch runtime validation for such a composition and directs
the operator to a separately provisioned test application. Normal preflight,
plan and activation remain available. The default `"scratch"` preserves existing
providers that keep state within Core's allocated application home.
