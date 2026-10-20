---
name: postgres-upgrades
description: >
  Plan and run PostgreSQL upgrades: applying minor releases (and their
  reindex or manual steps), choosing a major-upgrade method (dump/restore,
  pg_upgrade --link, logical replication blue/green cutover), managed-cloud
  upgrade paths, upgrading replicas, post-upgrade ANALYZE and plan
  regressions, extension compatibility, and OS/glibc/ICU collation changes
  that require reindexing. Use when moving a Postgres database to a new
  minor or major version, or to a new OS image.
---

# Postgres upgrades

A playbook distilled from *Scaling Postgres*. Case studies, numbers, and
version notes are in [reference.md](reference.md). For general replication
setup, use `postgres-replication-ha`.

## Minor releases

- Apply them promptly. They carry security and data-corruption fixes.
- **Read the release notes' "updating" section every time.** Some minor
  releases require a `REINDEX` of specific index types, or other manual steps.
- Test in staging first. Where exposure allows, waiting a few days avoids
  being first to hit a regression. An out-of-cycle release usually means
  "upgrade now".
- Upgrade the poolers and extensions too.

## Choosing a major-upgrade method

| Situation | Method | Downtime |
| --- | --- | --- |
| Small database | `pg_dump` / `pg_restore` (parallel `-j`) | Proportional to size |
| Same OS image, minutes of downtime OK | `pg_upgrade --link` (or `--clone`; PG18 adds `--swap`) | Minutes, mostly independent of size |
| Large DB, near-zero downtime, or also changing OS/hardware | Logical replication to a new cluster, then cut over | Seconds (the cutover) |
| Managed cloud | Provider's in-place upgrade or blue/green feature | Varies; read the fine print |

The host's current preference for large production systems is the logical
replication cutover. It also rebuilds every table and index compactly, and
it sidesteps collation problems.

## pg_upgrade checklist

1. `pg_upgrade --check` against a copy, and confirm extensions are installed
   in the new version.
2. Stop the application and the old cluster. Run with `--link --jobs N`.
3. Replicas: either rebuild them, or use the documented rsync procedure for
   standbys. Don't just start them.
4. **`ANALYZE` immediately.**
   - Before PG18, planner statistics are not carried over. Run
     `vacuumdb --all --analyze-in-stages` to get rough statistics fast.
   - PG18 keeps them, except extended statistics, which still need
     `ANALYZE`.
5. Watch for plan regressions in `pg_stat_statements` and compare against
   pre-upgrade baselines.

## Logical replication cutover

1. Build the new cluster on the target version and OS. Copy the schema
   (`pg_dump --schema-only`).
2. Create a publication on the old cluster and a subscription on the new one.
   Every table needs a primary key or a replica identity.
3. Wait for the initial sync and for lag to reach about zero.
   - **Freeze DDL**, since DDL isn't replicated.
   - Large objects aren't replicated either.
4. Verify: row counts, checksums on key tables, and application smoke tests
   against the new cluster.
5. Cutover:
   1. Pause writes (pooler `PAUSE`, or app maintenance mode).
   2. Wait for zero lag.
   3. **Set sequences** on the new cluster past the old values.
   4. Switch connections.
   5. Resume.
6. Keep the old cluster for rollback, optionally with reverse replication.
   Rehearse the whole thing several times so the real cutover is boring.

## OS, glibc, and collation

- A new glibc (or ICU) version can silently reorder text collation. B-tree
  indexes on text then return wrong results, and unique indexes can admit
  duplicates.
- **If you upgrade the OS with a file-copying method** (pg_upgrade, physical
  replica, snapshot):
  - Reindex all text indexes with non-C collations, including expression and
    partial indexes on text.
  - Check range partitions on text.
- Logical replication and dump/restore rebuild indexes, so they avoid the
  problem.
- Track collation versions (`pg_collation`, `pg_database.datcollversion`) and
  consider ICU or the PG17 built-in provider for new clusters.

## After any upgrade

- [ ] `ANALYZE` done (including partitioned parents and extended statistics).
- [ ] Key query plans and latencies compared with the baseline.
- [ ] Extensions updated (`ALTER EXTENSION ... UPDATE`).
- [ ] Backups re-baselined: a new full backup on the new cluster or timeline.
- [ ] Old cluster retained until the rollback window closes.
