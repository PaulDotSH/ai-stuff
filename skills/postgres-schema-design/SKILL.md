---
name: postgres-schema-design
description: >
  Performance-oriented PostgreSQL schema design and review: primary keys and
  UUIDs, column types, JSONB vs columns, constraints and foreign keys, index
  choice, update-heavy tables and MVCC bloat, partitioning, queues,
  pagination, counting, rollups, time-series, and multi-tenancy. Use when
  designing new Postgres tables, reviewing a schema or a migration's target
  shape, choosing indexes, or diagnosing a design-level performance problem.
  For how to apply a change to a live database without locking it, use
  postgres-safe-migrations.
---

# Postgres schema design

Guidance distilled from 439 episodes of *Scaling Postgres* (2018–2026). The
full reference, with evidence and episode citations, is
[design-guide.md](design-guide.md). Read the sections that match the task, not
the whole file.

## Scope

- In scope: what the schema should look like for performance and operability.
- Out of scope: the safe order and lock handling for changing a live schema.
  Use `postgres-safe-migrations` for that.
- Out of scope: query tuning on an unchanged schema (EXPLAIN reading, planner
  settings). Mention the relevant design lever if one exists, but don't turn a
  query question into a redesign.

## Workflow

1. Identify the workload of each table you touch:
   - insert-only or append
   - update-heavy
   - read-mostly
   - queue
   - time-series
   - multi-tenant
   The right answer differs per type.
2. Read the matching sections of `design-guide.md`.
3. Apply the review checklist below to every table in scope.
4. Report findings as concrete changes, each with the reason (the mechanism),
   not just the rule. Flag trade-offs where the guide gives them.
5. When existing tables would need to change, hand the "how to ship it"
   question to `postgres-safe-migrations`.

## Review checklist

Keys and types
- PKs are `bigint` identity or UUIDv7. Flag `int`/`serial` keys on tables that
  will grow, and random UUIDv4 PKs.
- `timestamptz`, not `timestamp`.
- `text` (plus a CHECK if needed), not `varchar(n)`.
- Money is integer minor units or `numeric`, never float.
- FK and join columns have identical types on both sides.
- For a status-like column, an enum only if its values are truly frozen;
  otherwise CHECK or a lookup table.

Shape
- Fields that are filtered, joined, constrained, or range-queried are real
  columns, not JSONB keys.
- No entity-attribute-value tables, and no delimited lists in a column.
- Large or medium text and JSONB that is read rarely or updated often lives in
  a side table, not in a hot row.
- Hot, frequently-updated columns (counters, last_seen, status) are split from
  wide rows, or the table is designed insert-only.

Constraints
- Every FK has an index on the referencing column, unless parents are never
  deleted and children are never looked up by parent.
- Upserts use `ON CONFLICT`, not insert-then-catch.
- Partitioned tables include the partition key in the PK and unique keys.

Indexes
- Composite indexes have equality columns first and range/sort columns last.
- Low-cardinality or skewed filters use partial indexes.
- No frequently-updated column sits in an index without a measured read win,
  because it kills HOT updates.
- Count the indexes per table. Each one taxes every write and adds a
  planning-time lock (watch the 16 fast-path locks before PG18).
- BRIN only for large, append-only, physically ordered data.

Lifecycle
- Data that expires is partitioned by the deletion key, so retention is a
  partition drop, not a `DELETE`.
- Partition counts stay in the hundreds, and queries filter on the partition
  key.
- Queue tables have a plan for dead tuples (see the guide's queue section).
- Lists use keyset pagination, and nothing computes an exact `count(*)` over a
  large set per request.

Tenancy
- `tenant_id` is on every table and leads tenant-scoped keys and indexes.

## Calibration

- Numbers in the guide come from specific workloads covered on the show.
  Quote them as examples, not guarantees.
- Prefer the simple design until the workload says otherwise. Don't
  pre-partition, pre-shard, or add rollup tables to a table that is small and
  will stay small.
