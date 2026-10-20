---
name: postgres-backup-recovery
description: >
  Plan, implement, and verify PostgreSQL backups and recovery: physical base
  backups with continuous WAL archiving, point-in-time recovery (PITR),
  pg_basebackup and PG17 incremental backups, pg_receivewal, backup tools
  (pgBackRest, Barman, WAL-G), snapshots, delayed replicas, restore testing,
  RPO/RTO, data checksums and corruption checks. Use when designing a backup
  strategy, restoring to a point in time, debugging WAL archiving or a
  filling pg_wal, or checking whether existing backups are trustworthy.
---

# Postgres backup and recovery

A playbook distilled from *Scaling Postgres*. Evidence, tool status and history
are in [reference.md](reference.md). For failover and replicas, use
`postgres-replication-ha`.

## Strategy

1. **Write down your RPO and RTO first.** They pick the tooling.
   - How much data you can lose sets WAL archiving and streaming frequency.
   - How long a restore may take sets backup size, parallelism, incrementals,
     and standbys.
2. **The real backup is a physical base backup plus continuous WAL archiving.**
   That is what gives point-in-time recovery.
   - `pg_dump` is a logical export for small databases, schema moves, and
     cross-version copies. It is not a backup strategy at scale.
3. **Use a backup manager,** such as pgBackRest or Barman, for retention, WAL
   expiry, parallelism, compression, verification, and restores.
   `pg_basebackup` only takes the copy. Check each tool's current maintenance
   status before adopting it.
4. **Add a delayed replica (or rely on PITR) for human mistakes.** A normal
   replica replays a bad `DELETE` immediately.
5. **Test restores on a schedule, automatically.** A backup you haven't
   restored is a hypothesis. Measure the restore time against your RTO.

## WAL archiving

- Keep every WAL segment from the start of the base backup onward.
- A failing or slow `archive_command` makes `pg_wal` grow until the disk fills
  and the server stops. Alert on `pg_stat_archiver.failed_count`,
  `last_failed_time`, and `pg_wal` size.
- Archiving must be faster than WAL generation at peak. Use parallel or async
  archiving in the tool, or an `archive_library` (PG15+).
- `pg_receivewal` streams WAL with less loss than file-by-file archiving.
  Pair it with a replication slot, and watch that slot.

## PITR, in outline (PG12+)

1. Restore the base backup into an empty data directory.
2. Set `restore_command`, plus a `recovery_target_time` /
   `recovery_target_lsn` / `recovery_target_name`, and
   `recovery_target_action` (`pause` lets you inspect before promoting).
3. Create `recovery.signal` (not `recovery.conf`, which went away in PG12).
   Start the server, verify the data, then promote.
4. After promotion a new timeline starts. Take a fresh base backup.

## Integrity

- Enable data checksums. `initdb --data-checksums`, or `pg_checksums --enable`
  offline. Newer versions enable them by default.
- Run `amcheck` (`bt_index_check`, `heapallindexed`) periodically and before
  trusting an old backup. Keep at least one known-good backup outside the
  corruption window.
- After an OS or glibc change, text indexes may be silently misordered. That
  is a reindex job, not a restore job (see `postgres-upgrades`).

## Checklist

- [ ] Base backups plus WAL archiving to separate storage, encrypted, with
      retention matched to the PITR window.
- [ ] Archive failure and `pg_wal` growth alerts.
- [ ] Automated restore test with a measured duration.
- [ ] Delayed replica, or a documented PITR runbook, for "someone dropped a
      table".
- [ ] Checksums on, with a periodic `amcheck`.
- [ ] Snapshot backups (cloud or ZFS) taken consistently, with
      `pg_backup_start`/`pg_backup_stop` or a tool that handles it, and
      restore-tested like any other backup.
