# Postgres safe migrations: evidence and history

Background for [SKILL.md](SKILL.md), distilled from *Scaling Postgres*
transcripts. `[NNN]` is the episode number. Lines marked *[uncited]* are
standard Postgres behavior that the transcripts don't spell out.

## 1. The lock queue

- **Most `ALTER TABLE` forms take ACCESS EXCLUSIVE** [321][231][432].
  - That includes ADD/DROP COLUMN, changing a default, type changes,
    TRUNCATE, CLUSTER, and VACUUM FULL.
  - It conflicts with plain `SELECT`.
  - A bare `LOCK TABLE` also defaults to ACCESS EXCLUSIVE [54].
- **How a "fast" DDL causes an outage** [321][419][83][1]
  1. A long query or open transaction holds a weak lock.
  2. The DDL queues behind it.
  3. Every later query queues behind the DDL.
- **Incidents**
  - An `ADD COLUMN` with no default "stopped everything for many minutes"
    behind one long query [83].
  - A bare `ADD COLUMN` hung about 20 minutes behind a long backfill
    transaction [1].
  - A migration run during `pg_dump` caused heavy locking [411].
- **`lock_timeout` is the fix, not `statement_timeout`.** A commenter
  corrected an early post on this point [83]. It is repeated across many later
  episodes [349][300][366][363][339][294][173].
- **Values used in practice**
  - 900 ms, deliberately below the 1 s `deadlock_timeout` [294].
  - 2 s [1].
  - 2–5 s depending on load [173].
  - 1 s with retries [321][274].
  - 50 ms `lock_timeout` plus 100 ms `statement_timeout`, with many retries,
    to change a column default on hot tables during a UUIDv7 migration. Cold
    tables went through easily [432].
- **Retries**
  - Use exponential backoff plus jitter, and avoid savepoint-heavy retry loops
    [185].
  - postgres.ai published both DO-block and procedure versions of a
    `lock_not_available` retry loop [347].
  - Benchling auto-retried only single-change migrations: 2 minutes apart, up
    to 10 tries. Over 3 months every migration went through with no manual
    intervention [51].
  - PG19 `REPACK CONCURRENTLY` attempts its final lock once, so a lock timeout
    aborts the whole repack. `pg_repack` retries [437].
- **Diagnosis**
  - `pg_blocking_pids()` [349].
  - Count ungranted rows in `pg_locks` [222].
  - Enable `log_lock_waits` to catch transient blockers [300].

## 2. Timeouts and roles

- **`statement_timeout` per role, not globally** [259][252][173]
  - App roles get about 30 s.
  - Migration and DBA roles get much more, or none.
  - Early advice was a 60 s database-wide default [118].
- **Migration role** [96][396]
  - Run migrations as the owner role. The app role can't run DDL.
  - Give the migration role an aggressive `lock_timeout`.
  - Connect it directly, not through PgBouncer.
- **Long transactions block DDL and vacuum alike**
  [411][129][398]
  - Guard with `idle_in_transaction_session_timeout`.
  - Guard with `transaction_timeout` (PG17).
- **Concurrent index builds and timeouts.** One Rails shop saved
  `statement_timeout`, set it to 0 for the build, then restored it. It also
  disabled the DDL transaction wrapper [147].

## 3. Columns: metadata-only vs rewrite

- **PG11 fast defaults** [28][35][231]
  - `ADD COLUMN` with any non-volatile default is catalog-only. It stores
    `atthasmissing` and `attmissingval`.
  - It finished in milliseconds on 100M rows [231].
  - Before PG11, adding a column with a default rewrote the table, and the
    advice was to add it bare and backfill [1][6].
- **Volatile defaults rewrite every row under ACCESS EXCLUSIVE.** Over a
  minute in one demo [231][351][350].
  - A team that needed a NOT NULL `uuidv7()` column on a huge table built a
    new table and swapped it in instead [399].
- **`DROP COLUMN`**
  - About 20 ms on a table with a big column, and the space stays allocated
    [101].
  - Dropped columns still count toward the 1,600-column limit [364].
  - Space comes back only through a rewrite (VACUUM FULL, pg_repack,
    pg_squeeze, or a logical move) [364][405].
- **Other rewrites:** `SET TABLESPACE`, `SET LOGGED/UNLOGGED`, `CLUSTER`,
  `VACUUM FULL`, type changes, and column reordering [146][346].
- **PG17 `ALTER COLUMN ... SET EXPRESSION`** on a generated column rewrites the
  table [299].
- **Rewrite test:** compare `pg_relation_filenode()` before and after [351].
- **varchar widening.** `varchar(n)` → larger `n` or `text` is binary-coercible
  and doesn't rewrite *[uncited]*. Prefer `text` plus CHECK from the start
  [253].
- **Views and column changes**
  - Views block type changes and drops of columns they use [81].
  - Views survive renames, because they reference objects by OID [81].
  - `SELECT *` in a view freezes the column list at creation [417].

## 4. Constraints

- **`NOT VALID` then `VALIDATE`** [350][322][217][147][199]
  - The ADD takes a brief lock and enforces the constraint on new rows.
  - The VALIDATE scans without blocking writes.
  - Lock levels *[uncited]*:
    - ADD FK takes SHARE ROW EXCLUSIVE on both tables.
    - VALIDATE takes SHARE UPDATE EXCLUSIVE.
- **Order of operations** [350]
  1. Stop the app writing violating data.
  2. Clean existing rows.
  3. Add `NOT VALID`.
  4. Validate.
- **NOT NULL**
  - PG12+ uses a validated `CHECK (c IS NOT NULL)` to skip the scan in
    `SET NOT NULL` [217][350].
  - PG18 makes NOT NULL a named constraint that can be added `NOT VALID`
    [385][386].
  - [199] claimed NOT NULL is lock-free on PG11+. That only holds for a new
    column with a default.
- **FKs on partitioned tables**
  - FKs referencing partitioned tables arrived in PG12 [61].
  - `NOT VALID` FKs on partitioned tables arrived in PG18 [386].
- **CHECK constraints must stay immutable.** Cross-table or mutable logic
  breaks `pg_restore` ordering [257].
- **An 18-case "schema change mistakes" taxonomy** [217] includes:
  - `IF NOT EXISTS` masking ordering bugs
  - timeouts seen only in production
  - unbounded single-transaction changes
  - mixing data copies with DDL
  - careless FK, CHECK, and NOT NULL additions
  - 4-byte PKs on large tables

## 5. Indexes

- **Always `CONCURRENTLY`.** It is the "golden rule" [1] and easy to forget
  [339][363].
- **Costs of concurrent builds** [349]
  - They take longer.
  - They are non-transactional and can't be rolled back.
  - Wrappers that open a transaction, like DO blocks, break them [185].
- **Failures leave INVALID indexes** [336][399][217]
  - Causes include deadlocks, disk full, duplicate keys, and cancellation.
  - **Incident [336]:** a per-partition concurrent build loop ran in an
    unmonitored job. One child deadlocked, its index stayed invalid, and
    queries slowed for a long time unnoticed.
  - An invalid-but-ready index (`indisready`) is still maintained on every
    write. It generates WAL, blocks HOT updates, and adds planning cost. One
    that never became ready isn't maintained [399].
  - Never combine `IF NOT EXISTS` with a concurrent build in migrations [190].
- **`REINDEX CONCURRENTLY`**
  - Added in PG12 [57][83]. It works on partitioned tables from PG14 [131].
  - In one case it cut a bloated index's reads from about 24–25K buffers to 33
    [405].
  - PG14.0–14.3 had a bug where concurrent builds could miss HOT-updated
    tuples, causing silent corruption. It was fixed in 14.4. Verify with
    `amcheck` and rebuild [218][219][220].
- **Partitioned parents**
  - No concurrent build on the parent, and no concurrent drop of a parent
    index. That is an argument against partitioning early [358][353][414].
  - The `ON ONLY` plus `ATTACH PARTITION` sequence is *[uncited]*. The
    transcripts describe building on the children first, after which the
    parent index "creates quickly" [358].
- **Collation changes**
  - A glibc upgrade (2.28 especially) or a replica on a different OS corrupts
    text index order [72][231][397].
  - Affected objects:
    - text indexes with non-C collations
    - expression and partial indexes on text
    - string range partitions
    - CHECKs comparing strings
  - Fixes: `REINDEX CONCURRENTLY`, or do the OS move through logical
    replication [397].

## 6. Type changes and backfills

- **int → bigint**
  - Trigger plus batched backfill plus concurrent unique index plus a swap
    transaction [315][347][400][274].
  - Percona held the swap lock for under 1 s, and backfilled 5,000 IDs at a
    time from a helper table [274].
  - Variant: add the new column `NOT NULL DEFAULT 0` to skip the NOT NULL
    steps [400].
  - Emergency variant: swap first with a `NOT VALID` NOT NULL check, then
    backfill [255].
  - Procedures can `COMMIT` inside a backfill loop [255].
- **Avoid one giant UPDATE.** A 76 s update caused lock contention, frequent
  checkpoints with full-page writes, and bloat [217].
- **Batch size**
  - 1,000-row keyset batches [333]
  - 5,000 [274]
  - the host's approach: start at 1,000 and ramp to 5–20K while watching
    production [313]
- **Between batches,** vacuum and analyze. Reindex afterwards if bloat is large
  [217][313][400].
- **CTID batching** gave a 30x speedup on a 200M-row, 40 GB static table. A
  `processed` flag plus a partial index was slow [414].
- **Replica lag during backfills.** Raising `maintenance_io_concurrency` on the
  replica (about 16 vs the default 10) reduced replay lag [241].

## 7. Renames, swaps, process

- **Column renames.** The 6-stage expand/contract rename is "onerous". The
  host's advice is to skip it, or fold it into a brief window [285].
- **Versioned-schema views** let old and new app versions coexist [171].
  `pgroll` builds on this idea [285].
- **Table rename with an updatable view shim** [240].
- **Copy-and-swap**
  - A real case: trigger sync plus batched copy plus a name swap in one
    transaction took 4–6 hours with zero downtime [144].
  - Grants, RLS, triggers, FKs, publications, statistics objects, and
    dependent views are tied to the OID [399].
- **Tooling**
  - `strong_migrations` flags dangerous operations but doesn't add
    `lock_timeout` [294][269].
  - Nandi and `active_record_safer_migrations` set lock and statement timeouts
    and automate the FK steps [105].
  - Benchling split migrations into pre-deploy and post-deploy sets [51].
- **Rehearse on production-like data;** thin clones help [217][197].
- **Roll forward rather than back** [269].
- **Logical replication doesn't replicate DDL or sequences.** Apply schema
  changes on subscribers separately [370].

## 8. Enums and partitions

- **Enum history** [333]
  - ADD VALUE without a rewrite (9.1).
  - RENAME VALUE (PG10).
  - ADD VALUE inside a transaction (PG12).
  - The new value usable in the same transaction (PG17).
  - Values still can't be dropped or reordered. The workaround is renaming to
    a "deleted" marker [395].
- **Partitions**
  - Add a matching CHECK before `ATTACH` to skip validation [358].
  - Keep the default partition empty [358].
  - Default partitions block `DETACH CONCURRENTLY` [395][247].
  - Anti-wraparound autovacuum won't yield to partition DDL [419].
  - MERGE/SPLIT PARTITIONS was reverted from PG17 and returns in PG19 with
    ACCESS EXCLUSIVE on the parent [418][399].
  - Repartitioning via logical replication is possible but risky [118].

## 9. Replicas and other side effects

- ACCESS EXCLUSIVE locks replay on standbys and cancel queries there. Vacuum's
  truncate step also takes one, so consider `vacuum_truncate = off` [140].
- `NOTIFY` serializes commits with a global lock. Throughput collapsed in one
  incident [375][426].
- `pg_repack` takes brief ACCESS EXCLUSIVE locks in about three places. Use
  `--no-kill-backend` [171].
- PG19 `REPACK CONCURRENTLY` [414][437]:
  - It copies under SHARE UPDATE EXCLUSIVE using logical decoding.
  - Only one can run at a time.
  - Its replication slot holds back vacuum.
  - Repeatable-read transactions that started before the swap see zero rows.

## How the advice moved

| Topic | Older | Current |
| --- | --- | --- |
| Column with default | Add bare, backfill (pre-PG11) | Constant/non-volatile default is instant |
| Timeouts | `statement_timeout` around DDL | `lock_timeout` + retries, per-role statement timeouts |
| NOT NULL | Use a CHECK instead | CHECK → validate → SET NOT NULL (PG12); `NOT VALID` NOT NULL (PG18) |
| Reindex | Build new concurrently, drop old, rename | `REINDEX CONCURRENTLY` (PG12; partitioned PG14) |
| Enum ADD VALUE | Outside transactions | Inside (PG12), usable same txn (PG17) |
| Column rename | Expand/contract | Often: don't, or brief window |
| Online table rewrite | pg_repack / pg_squeeze | + PG19 `REPACK CONCURRENTLY` (with caveats) |
