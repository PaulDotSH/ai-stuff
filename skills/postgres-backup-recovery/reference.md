# Postgres backup and recovery

Distilled from *Scaling Postgres* episodes (2018–2026) and three standalone
tutorials, from auto-generated transcripts. `[NNN]` is an episode number;
`[tutorial: ...]` is a standalone tutorial (written for PG10, so its
`recovery.conf` steps are superseded, see section 5). Many numbers come from
third-party posts, so read them as "this happened on one workload". Where advice
changed, the newest take wins. Replication and failover belong to
postgres-replication-ha; upgrades are out of scope.

## Checklist

- **Strategy:** a physical base backup plus continuous WAL archiving (PITR) is
  the real backup. `pg_dump` is an export tool for small databases, schema
  moves and cross-version copies.
- **Test restores:** schedule and automate them. A backup is only what you have
  restored.
- **WAL:** keep every segment from backup start onward. Alert on archive lag
  and on `pg_wal` growth.
- **Tooling:** `pg_basebackup` only takes the backup. Use pgBackRest or Barman
  (or your own scripts) for retention, WAL handling and verification.
- **Mistakes:** a delayed replica or PITR recovers from a bad `DELETE`. A
  normal replica does not.
- **Corruption:** enable data checksums, run `amcheck`, keep a known-good
  backup.
- **State your RPO and RTO** before choosing a tool.

## 1. Logical (pg_dump) vs physical

**pg_dump is not a backup strategy at scale** [035][331][338]
- A dump restores only to the moment it started. A physical backup plus WAL
  restores to any point [035].
- Dumps get slow and resource-heavy on large databases [035]. One 1 TB
  `pg_dump`/`pg_restore` took about 17 hours, which is your migration
  downtime [406].
- PG18 docs reword "backup" to "export". The host now calls `pg_dump` an
  export/import tool: it has no repository, retention or WAL handling
  [331][338].
- Small databases with loose recovery needs: dumps are acceptable, and moving
  up to `pg_basebackup` and then PITR is the path as needs grow [332].
  Whatever you use, test the restore [338].

**What pg_dump is good for** [341][094]
- It is portable across versions and architectures, editable, and can rescue
  tables from a corrupted database [341].
- A per-database dump omits globals (roles); `pg_dumpall` covers the cluster
  but was text-only [331].
- Better practice [094]: dump roles and tablespaces separately, then each
  database in directory format. That allows `-j` parallel dump and restore and
  single-table restore.

**Compression** [263][254]
- zstd was added to `pg_dump`. In one test it produced 8 MB vs 48 MB for lz4,
  about three times smaller than gzip [263].
- lz4 is faster, gzip smaller [254].

**Restore gotchas**
- `pg_restore` and `pg_upgrade` do not refresh statistics. Run `vacuumdb` with
  `--analyze-only` and `--jobs` [339].
- Unlogged tables are dumped and restored on the primary but never reach
  replicas [356].

## 2. Physical backups: pg_basebackup

**PITR basics** [tutorial: PITR]
- It needs live-file backups plus archived WAL.
- It backs up and restores the whole cluster, not single databases or tables.
- It runs online and cannot be used to upgrade.
- `wal_level` must be at least `replica`.

**Keep the WAL that goes with the backup** [389]
- Clients retained `pg_basebackup` output without the matching WAL, and the
  backup could not be restored. You need WAL from backup start until a
  consistent point.
- Default `pg_basebackup` includes it; custom setups that archive WAL and
  backups separately often break this [389].
- `-X none` leaves WAL out, so you then manage it yourself [113].

**Verification** [113][336]
- PG13 writes a backup manifest (JSON: files, WAL ranges, checksums) that
  `pg_verify_backup` checks.
- PG18 adds verification of tar and compressed backups [336].

**Compression** [214][288][202][219][309]
- PG15 added server-side compression (gzip, lz4, zstd) as well as client-side
  [288].
- Use `tar` output: `plain` took about twice as long because the client must
  decompress [214].
- The host's practice for years: pipe `pg_basebackup` into `pigz` or `zstd` to
  use all cores [214][219]. zstd used about a quarter of gzip's CPU for the same
  result [219].

**Speed and cache** [392][438]
- `pg_basebackup` is single-threaded: 300–500 Mbit/s on PG17, about 1 GB/s on
  PG18 with async I/O (worker method) [392].
- pgBackRest with 32 processes reached about 10 Gbit/s on a 128-vCPU box,
  almost 10x faster, used to seed replicas [392].
- Backups evict hot data from the page cache. On a half-terabyte database,
  buffered reads evicted 40 GB; WAL-G's direct I/O evicted none and gave about
  3x better query p99. Size the direct read to the stripe [438].

**Backup labels and the low-level API** [268][238][287]
- Exclusive backup mode was removed in PG15. The functions are now
  `pg_backup_start` and `pg_backup_stop`, so old scripts break [238].
- With the low-level API, save what `pg_backup_stop` returns as
  `backup_label`. Without it, recovery relies on the control file and can start
  from the wrong point when several checkpoints occur during the backup [268].
- Omitting `backup_label` is a documented way to corrupt a restore [229].

## 3. Incremental backups (PG17+)

**Mechanism** [297][298]
- Enable WAL summarization (`summarize_wal`) before the first full backup.
  Summaries, one file per checkpoint cycle, live under `pg_wal/summaries` and
  record which blocks changed [298].
- `pg_basebackup` takes the delta against the previous backup's manifest.
  `pg_combinebackup` merges full plus incrementals into a restorable
  directory [297].
- Patterns: weekly full with daily incrementals, or daily full with
  incrementals every few hours. WAL still takes you to a point in time [297].
- Combine Monday's incremental into Sunday's full to get a new full [298].

**Caveats**
- It is low-level: no inventory, schedule or retention, so you still need
  Barman, scripts or similar [384].
- PG18 adds a link mode for combining, avoiding a rewrite [384].
- Chain fragility: one lost or corrupted file invalidates everything
  downstream; pgHardStorage rejects chains for this reason [425].
- The committer reported many fixes, some for data-corrupting bugs, after it
  landed [315].

## 4. WAL archiving

**archive_command and archive_library**
- Setup: `archive_mode = on`, a restart, an `archive_command`, then a manual
  `pg_switch_wal()` to test [tutorial: PITR].
- PG15 added loadable archive modules (`archive_library`). `basic_archive` is
  an example, not production-ready [202][244]. Barman and pgBackRest were
  expected to adopt modules [202].
- `archive_command` waits for a full segment (16 MB by default), so committed
  transactions in the open segment can be lost in a disaster [087].

**Slow archiving fills pg_wal** [143][131]
- A slow `archive_command` (S3 copy, remote host) backs up segments: 278 were
  waiting in one pgbench run, 166 with compression [143].
- Fix: an asynchronous archiver (pgBackRest) acknowledges immediately and
  uploads with background workers. You must still verify it keeps up
  [143][131].
- On restore, prefetch WAL into a spool. Fetching one segment per round trip
  from S3 can leave a replica unable to catch up [176][167].
- `pgbackrest info` does not confirm all WAL is present; its `check` does
  [057].
- The `wal_compression` setting costs little CPU and saves disk and network on
  busy systems [101].

**Disk full** [161][323]
- Four causes: failing `archive_command`, an orphaned slot or dead standby, a
  mass write, or no monitoring [161].
- Never delete WAL. "The data directory was full so we deleted the WAL files"
  is a cited misuse [161][323].
- Cap slots with `max_slot_wal_keep_size`; one example used 100 GB [270].
- Run WAL-archive cleanup only against the archive destination, never the data
  directory [072].

**pg_receivewal** [tutorial: pg_receivewal][087][119]
- It streams WAL over the replication protocol to another host, offloading the
  primary and avoiding the wait for a full segment [087]. Use a replication
  slot; a compression option (0–9) arrived in PG10.
- Run it under systemd so it restarts.
- The newest segment is a `.partial` file. Rename it to a full segment before
  restoring, or replicas and the receiver cannot follow after the restore
  [tutorial: restore with pg_receivewal].
- A restore does not bring back replication slots. Recreate them, or the
  replica and receiver error out [tutorial: restore with pg_receivewal].

## 5. PITR procedure

**Steps** [tutorial: PITR]
1. Stop the server and clear the data directory (test systems only).
2. Extract the base backup (in tar format, also the `pg_wal` tar).
3. Make archived WAL available through `restore_command`.
4. Set a recovery target and start.

**PG12 and later** [085][095]
- `recovery.conf` is gone. Put settings in `postgresql.conf` (or
  `postgresql.auto.conf`) and create `recovery.signal` for archive recovery, or
  `standby.signal` for standby mode. Standby wins if both exist [085].
- Only one `recovery_target_*` parameter may be set. Older versions took the
  last one [085].
- `pg_basebackup -R` writes `standby.signal` and appends to
  `postgresql.auto.conf` [085].
- Check that your backup tool supports PG12: pgBackRest needs 2.18 or later
  [092].
- Targets: latest, an LSN, or a timestamp [265].
- At the target, recovery pauses read-only. Check the data, then call
  `pg_wal_replay_resume()` [tutorial: PITR].
- A PITR starts a new timeline [190].

**Traps**
- An 18 TB PITR replay stalled silently, with no I/O or CPU. The post blamed a
  replay race condition for which a Postgres fix exists [424].
- Run `ANALYZE` after restore [339].

## 6. Tools and their status

**pgBackRest**
- Archived read-only on 27 April with a notice of obsolescence. The sole
  maintainer had worked on it for 13 years under a corporate sponsor [415].
- Cited strengths [415]: block-level incremental, parallel restore, page
  checksum validation, S3/Azure/GCS, encryption.
- A flood of user demand made continuation "all but certain" [416], and the
  project announced it will continue, with sponsors including AWS, Supabase,
  pgEdge, Tiger Data and Percona [418]. A fork, PGX backup, was prepared as a
  fallback [416]. The host calls it healthier now [420].
- The lesson: a solo-maintained project had no structural protection [415].

**Barman** (EDB). Cloud WAL archive and backup [099][123][124], geo-redundant
copies [052]. EDB's advice: use Barman or pgBackRest rather than reinventing
them [332][272].

**WAL-G.** Continuous backups [126] with a direct I/O option [438].

**pgHardStorage** (Cybertec, v1, Apache 2.0, PG15+) [423][426][425][427]
- A separate host connects via a replication slot, so there is no
  `archive_command`. It chunks and dedupes, encrypts, and stores to object
  storage or disk. Every backup is effectively full, with no incremental chain
  [423][425].
- It needs a replication role with `pg_read_all_data`, and works on managed
  Postgres [423][426].
- No scheduler (use cron) or web UI. It reads the whole data directory on each
  backup; a snapshot-aware fast path is planned [426][425].
- Migrate by dual-writing to a new repository beside the existing one [427].

**Roll your own.** Scripts around `pg_basebackup` and `pg_combinebackup` are a
viable path on PG17+ [415][427]. `pg_basebackup` has no restore tool and no
repository of what exists [415].

## 7. Snapshots and cloud

- Bracket a filesystem or EBS snapshot with `pg_backup_start` and
  `pg_backup_stop` [287]. That gives no PITR unless you add WAL handling [278].
- Snapshots restore fast: CloudNativePG restored a 4.5 TB database in 2 min
  2 s, snapshotting a stopped replica; the large backup itself took hours
  [287].
- Around 10 TB, people move to snapshots, but cloud snapshots lock you in, so
  keep another backup [359].
- Validate snapshot restores carefully [176].
- Backup time scales with shard count: at 40 TB, one instance takes about
  40 h, 8 shards 2.8 h, 32 shards 42 min [429].
- Planning rule: about one hour of downtime per 200 GB restored [259].

## 8. Delayed replicas

- `recovery_min_apply_delay` keeps a replica behind (8 hours in two reported
  setups) [053][132][173].
- After a mistaken `DROP` or `DELETE`, pause replay, roll forward to just
  before the incident, and copy the data out. That is faster than restoring
  terabytes [053][173].
- Replication is not a backup; a normal replica replays the mistake at once
  [053].

## 9. Restore testing, RPO and RTO

- Restore after every backup, with whatever tool [332][240]. Reinstall the same
  extensions and packages [240].
- Automate it: a dedicated restore server that restores weekly [376][054]. Run
  a verification query on the restored data. For dump restores, disable
  triggers that call external systems. Test PITR too [102].
- Alert when backup size is zero or drifts, and restore periodically. At GitLab
  none of five backup mechanisms worked [018].
- RPO and RTO are negotiated against each other; match the recovery path to the
  incident type [422].

## 10. Checksums and corruption

- Enable data checksums and run `amcheck` regularly [363][366]. PG12 added a
  way to enable checksums on an existing cluster [060].
- `pg_stat_database` counts checksum failures, including during base backup
  [060]. On a clone of production, `pg_checksums` shows where the damage is
  [363].
- Response order [363]:
  1. Stop Postgres to prevent further damage.
  2. Restore a known-good backup with PITR.
  3. Salvage tables by dump.
  4. Use `pg_resetwal` only as a last resort; expect data loss since the last
     checkpoint.
- If you must use `pg_resetwal`, back up first and never call it carelessly
  [286].

## How the advice moved

| Topic | Older | Current |
| --- | --- | --- |
| pg_dump | "backs up a database" | Export tool; PITR is the backup (PG18 docs say "export") |
| Exclusive backup | `pg_start_backup`/`pg_stop_backup` | Removed in PG15; `pg_backup_start`/`pg_backup_stop` |
| Recovery config | `recovery.conf` | PG12: `recovery.signal` / `standby.signal` and GUCs |
| Incremental | pgBackRest or Barman only | PG17 incremental plus `pg_combinebackup`; link mode in PG18 |
| pgBackRest | "best tool" | Archived, then continued with sponsors; alternatives are Barman, WAL-G, pgHardStorage |
| Archiving | shell `archive_command` | Async archiver or `archive_library`; or `pg_receivewal` |
| Compression | gzip, pipe to pigz | Server-side zstd or lz4 (PG15+) |
