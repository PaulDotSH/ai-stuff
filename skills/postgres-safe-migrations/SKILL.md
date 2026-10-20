---
name: postgres-safe-migrations
description: >
  Write and review PostgreSQL schema migrations that run on a live database
  without blocking traffic: lock_timeout and retries, the DDL lock queue,
  which ALTERs rewrite the table, adding columns/defaults/NOT NULL,
  constraints and foreign keys with NOT VALID + VALIDATE, CREATE/DROP INDEX
  CONCURRENTLY and invalid indexes, int-to-bigint conversions, batched
  backfills, renames, table swaps, enums, and partition attach/detach. Use
  whenever you write, generate, or review a migration (raw SQL, Rails,
  Django, Alembic, Prisma, Flyway, etc.) that targets a Postgres database
  with real traffic or large tables.
---

# Postgres safe migrations

A playbook distilled from *Scaling Postgres* (2018–2026). Evidence, incidents
and version history are in [reference.md](reference.md). For what the target
schema should look like, use `postgres-schema-design`.

## Ground rules

1. **Every DDL statement runs with a short `lock_timeout`, and the migration
   retries on timeout.**
   - Most `ALTER TABLE` forms need `ACCESS EXCLUSIVE`, even the instant ones.
   - If any transaction holds a conflicting lock, the DDL waits. Every later
     query on that table, plain `SELECT` included, then queues behind the DDL.
     A metadata-only change becomes an outage.
   - Pick a `lock_timeout` between 100 ms and 2 s. Lower it for hot tables.
     Keep it below `deadlock_timeout` (default 1 s) when you can.
   - Retry the whole migration step with backoff plus jitter.
2. **Set a `statement_timeout` for the migration session too,** so nothing
   runs unbounded.
   - Raise it, or set it to `0`, only for steps that are expected to be long
     and that don't block: `CREATE INDEX CONCURRENTLY`, `VALIDATE CONSTRAINT`,
     and backfill batches.
3. **One risky change per transaction or migration.**
   - Never put a slow data change in the same transaction as DDL. A backfill
     inside the DDL transaction holds the strong lock the whole time.
   - Single-change migrations are also safe to retry automatically.
4. **Separate schema changes from data changes.** Backfills run as their own
   batched job, not inside the migration transaction.
5. **Rehearse on production-sized data,** such as a restored copy or a thin
   clone. Timeouts and rewrites only show up at real size.
6. **Check for long-running and idle-in-transaction sessions before you
   start.** Look at `pg_stat_activity` (`xact_start`, `state`). Don't run DDL
   while `pg_dump` or a big batch job is running.
7. **Roll forward.** Design each step so it is safe to leave in place, rather
   than counting on a rollback.

Template for a migration step:

```sql
SET lock_timeout = '1s';          -- per session/transaction, not global
SET statement_timeout = '15s';
ALTER TABLE orders ADD COLUMN note text;   -- one change
-- on lock_not_available (SQLSTATE 55P03): roll back, sleep with jitter, retry
```

- Prefer retrying at the migration-runner level, which re-runs the whole
  transaction.
- A PL/pgSQL `LOOP` that catches `lock_not_available` also works. Each
  exception block is a subtransaction, and the outer transaction keeps any
  locks it already took, so keep that loop to a single statement.

## Operation cheat sheet

| Operation | Safe? | How |
| --- | --- | --- |
| `ADD COLUMN` nullable, no default | Instant | `lock_timeout` + retry |
| `ADD COLUMN ... DEFAULT <constant/non-volatile>` | Instant (PG11+) | `lock_timeout` + retry |
| `ADD COLUMN ... DEFAULT <volatile>` (`random()`, `clock_timestamp()`, `uuidv7()`) | **Rewrites table** | Add nullable, set default, backfill in batches, then NOT NULL recipe |
| `DROP COLUMN` | Instant (space not reclaimed) | `lock_timeout`; remove app references first |
| `ALTER COLUMN TYPE` (int→bigint, int→text, …) | **Rewrites table + indexes** | Expand/contract recipe below |
| `varchar(n)` → wider `varchar` / `text` | Instant [uncited, standard behavior] | `lock_timeout` |
| `SET DEFAULT` / `DROP DEFAULT` | Instant | `lock_timeout` (hot tables need tight timeout + many retries) |
| `SET NOT NULL` | **Full scan under ACCESS EXCLUSIVE** | CHECK `NOT VALID` → `VALIDATE` → `SET NOT NULL` (PG12+), or PG18 `NOT NULL ... NOT VALID` |
| `ADD CHECK` / `ADD FOREIGN KEY` | **Full scan under lock** | `NOT VALID`, then `VALIDATE CONSTRAINT` separately |
| `ADD UNIQUE` / `PRIMARY KEY` | **Index build under lock** | `CREATE UNIQUE INDEX CONCURRENTLY`, then `ADD CONSTRAINT ... USING INDEX` |
| `CREATE INDEX` | **Blocks writes** | `CREATE INDEX CONCURRENTLY` |
| `DROP INDEX` | Takes ACCESS EXCLUSIVE | `DROP INDEX CONCURRENTLY` |
| `REINDEX` | **Blocks writes** | `REINDEX ... CONCURRENTLY` (PG12+, partitioned PG14+) |
| Rename column / table | Instant, but breaks running app code | Expand/contract or a view shim (below) |
| `VACUUM FULL` / `CLUSTER` / `SET TABLESPACE` / `SET (UN)LOGGED` | **Rewrite under ACCESS EXCLUSIVE** | pg_repack / pg_squeeze, PG19 `REPACK CONCURRENTLY`, or a maintenance window |
| Enum `ADD VALUE` | Instant | OK in a transaction on PG12+; usable in the same transaction on PG17+; values can't be dropped or reordered |

**Not sure whether something rewrites?** Run it on a throwaway copy and compare
`SELECT pg_relation_filenode('t')` before and after. A new filenode means the
table was rewritten.

## Recipes

### NOT NULL on an existing column (PG12–17)

```sql
ALTER TABLE t ADD CONSTRAINT t_c_not_null CHECK (c IS NOT NULL) NOT VALID; -- brief lock
ALTER TABLE t VALIDATE CONSTRAINT t_c_not_null;  -- scans, doesn't block writes
ALTER TABLE t ALTER COLUMN c SET NOT NULL;       -- uses the validated CHECK, no scan
ALTER TABLE t DROP CONSTRAINT t_c_not_null;
```

- PG18+ allows `ALTER TABLE t ADD CONSTRAINT t_c_nn NOT NULL c NOT VALID;`
  followed by `VALIDATE CONSTRAINT`.
- First make sure the app no longer writes NULLs, and backfill existing rows.

### CHECK and foreign keys

```sql
ALTER TABLE child ADD CONSTRAINT child_parent_fk
  FOREIGN KEY (parent_id) REFERENCES parent(id) NOT VALID;  -- brief lock on BOTH tables
ALTER TABLE child VALIDATE CONSTRAINT child_parent_fk;      -- separate step
```

- Index `child(parent_id)` first, with `CREATE INDEX CONCURRENTLY`.
- Adding or dropping an FK locks both tables, so use `lock_timeout` on each.

### Unique constraint or primary key

```sql
CREATE UNIQUE INDEX CONCURRENTLY t_email_key ON t (email);
ALTER TABLE t ADD CONSTRAINT t_email_key UNIQUE USING INDEX t_email_key;
-- or: ADD CONSTRAINT t_pkey PRIMARY KEY USING INDEX t_pkey_idx (columns must be NOT NULL)
```

### Indexes, concurrently

- `CREATE/DROP INDEX CONCURRENTLY` and `REINDEX CONCURRENTLY` can't run inside
  a transaction block. Disable the framework's per-migration transaction:
  - Rails: `disable_ddl_transaction!`
  - Django: `atomic = False`
  - Alembic: `with op.get_context().autocommit_block()`
- A long build hits `statement_timeout`. Set it to `0` for that step only.
- **A failed concurrent build leaves an INVALID index.** It can still be
  maintained on every write, which costs WAL, blocks HOT updates, and adds
  planner work.
  - Check after every build:
    `SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;`
  - Clean up with `DROP INDEX CONCURRENTLY`, then rebuild.
- Never write `CREATE INDEX CONCURRENTLY IF NOT EXISTS`. After a failure it
  silently skips the rebuild and leaves the invalid index in place.
- Build the new index before dropping the one it replaces.
- Watch progress in `pg_stat_progress_create_index`.

### Partitioned tables

- **Indexes.** You can't build an index concurrently on the parent.
  1. `CREATE INDEX ... ON ONLY parent` (creates it invalid, instantly).
  2. `CREATE INDEX CONCURRENTLY` on each partition.
  3. `ALTER INDEX parent_idx ATTACH PARTITION child_idx` for each one.
  4. The parent index turns valid once every child is attached.
  5. Verify that every child build succeeded.
- **Dropping a parent index** is never concurrent; it locks every partition.
  Decide before you add one.
- **ATTACH a populated table.** Add a `CHECK` constraint matching the partition
  bound first, so `ATTACH PARTITION` skips the validation scan.
- **DETACH** with `DETACH PARTITION ... CONCURRENTLY` (PG14+). It can't run in
  a transaction block, and it fails if a default partition exists.
- **MERGE/SPLIT PARTITIONS** (PG19) holds ACCESS EXCLUSIVE on the parent for
  the whole operation. Use it only in a maintenance window.

### Changing a column type (e.g. int → bigint)

1. `ADD COLUMN new_id bigint` (with `lock_timeout`).
2. Add a trigger that copies `id` into `new_id` on INSERT/UPDATE.
3. Backfill in batches (see below).
4. `CREATE UNIQUE INDEX CONCURRENTLY` on `new_id`. Add NOT NULL via the
   CHECK recipe.
5. In one short transaction with `lock_timeout`:
   - drop the trigger and the old PK
   - rename the columns
   - `ADD CONSTRAINT ... PRIMARY KEY USING INDEX`
   - move the sequence/identity default
6. Repeat for every FK column that references it. Drop the old column later.

**Emergency, the sequence is nearly exhausted:** restart it in the negative
range to buy time.

```sql
ALTER SEQUENCE t_id_seq MINVALUE -2147483648 INCREMENT BY -1 RESTART WITH -1;
```

This is standard Postgres syntax, not taken from the transcripts. Code that
sorts by `id` to mean "newest" breaks, so use it only as a stopgap.

### Batched backfills

- Never run one giant `UPDATE`. It holds row locks, triggers checkpoint and WAL
  storms, lags replicas, and bloats the table.
- Walk the key in keyset batches:

  ```sql
  UPDATE t SET new_col = <expr>
  WHERE id > $last AND id <= $last + 5000 AND new_col IS DISTINCT FROM <expr>;
  ```

  - Start around 1,000 rows and ramp up while watching latency and replica
    lag. Aim for about 1 s of work per batch, then pause.
  - Commit each batch. Use a procedure with `COMMIT` in the loop, or a driver
    script. A function can't commit.
- Run `VACUUM` (and `ANALYZE`) between rounds on big backfills. Reindex
  afterwards if bloat is large.
- Static tables can be walked faster by `ctid` page ranges.

### Renames

- **Column.** Either take a brief planned window, or do
  expand → dual-write → backfill → switch reads → drop the old column. Often
  the right answer is not renaming at all.
- **Table.** In one transaction, rename the table and
  `CREATE VIEW old_name AS SELECT * FROM new_name`. Simple views are
  updatable. Deploy code that uses the new name, then drop the view.
- **Views and dependents.** Views reference columns by OID, so renames carry
  through. A type change or `DROP COLUMN` under a view fails until you drop
  and recreate the view.

### Copy-and-swap a table

Use it for volatile defaults, column reorders, or shrinking a huge table.

1. Create the new table.
2. Copy rows in keyset batches.
3. Keep it in sync with triggers for INSERT/UPDATE/DELETE.
4. Build indexes.
5. Set sequences.
6. Swap names in one transaction.

Grants, RLS policies, triggers, FKs, publications, statistics objects, and
dependent views/functions are tied to the old table's OID and do not follow
the name. Recreate them all. This needs about 2x disk.

## Review checklist for a migration

- [ ] Every DDL step sets `lock_timeout` and has a retry path.
- [ ] No `ALTER COLUMN TYPE`, volatile default, or `SET NOT NULL` on a large
      table without the recipes above.
- [ ] Constraints and FKs are added `NOT VALID` then validated.
- [ ] Indexes use `CONCURRENTLY`, outside a transaction. There is no
      `IF NOT EXISTS` on concurrent builds, and a check for invalid indexes
      follows.
- [ ] Data backfills are batched and separate from schema changes.
- [ ] App deploy order is compatible: expand before the code that uses it,
      contract after the code stops using it.
- [ ] Timed on production-sized data. Nothing long is running at deploy time.
- [ ] Partitioned tables: per-child concurrent indexes, a CHECK before
      ATTACH, and no default partition if you need concurrent detach.

## Tools

- `strong_migrations` (Rails) flags unsafe operations, but add `lock_timeout`
  yourself.
- `pgroll` does reversible, versioned-view migrations.
- `pg_repack` and `pg_squeeze` do online rewrites. They need about 2x disk and
  take brief ACCESS EXCLUSIVE locks.
- PG19's `REPACK CONCURRENTLY` makes a single swap attempt with no retry. It
  is not MVCC-safe for older repeatable-read snapshots.
