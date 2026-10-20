---
name: postgres-connection-pooling
description: >
  Size and configure PostgreSQL connections and connection pooling:
  max_connections, idle-connection and per-connection costs, PgBouncer pool
  modes and what transaction mode breaks (SET, advisory locks, temp tables,
  LISTEN/NOTIFY, prepared statements), PgBouncer sizing, auth, monitoring
  and multi-process setups, alternatives (PgCat, PgDog, Supavisor, RDS
  Proxy), timeouts, dead connections, and retry storms. Use when an app hits
  "too many clients", connection latency, pool exhaustion, or when adding or
  configuring a pooler.
---

# Postgres connections and pooling

A playbook distilled from *Scaling Postgres*. Evidence, benchmarks and
version history are in [reference.md](reference.md).

## Rules

- **Postgres forks a process per connection.** Connecting is expensive, and
  idle connections cost memory and snapshot work. Pool once you have more
  than a few hundred connections, or any connect-per-request clients.
- **Keep `max_connections` low.** About 10x vCPUs at most; thousands is a
  smell. Waiting in a pooler queue beats overloading the backend.
- **Server-side pools are small:** tens to low hundreds of backend connections
  behind thousands of clients. Size them for concurrency, then watch the wait
  queue.
- **Run application traffic in transaction mode.** Session mode saves connect
  time but not backends.
- **Keep a direct or session-mode path** for migrations (with
  `lock_timeout`), `LISTEN` workers, and admin work.
- **Patch the pooler** like the database. PgBouncer before 1.26 has CVEs
  reachable without authentication. Never expose it to the internet.

## Transaction mode: what breaks and the fix

| Feature | Problem | Fix |
| --- | --- | --- |
| `SET`, `SET search_path` | Leaks to other clients' transactions | `SET LOCAL` inside the transaction, or role/database-level settings |
| Session advisory locks | Lock outlives your transaction on a shared backend | `pg_advisory_xact_lock` |
| Temp tables | Bound to a backend | `ON COMMIT DROP` within one transaction, or unlogged tables |
| `LISTEN` | Needs a persistent session | Dedicated direct connection |
| Prepared statements | Named statements land on the wrong backend | PgBouncer ≥ 1.21 with `max_prepared_statements` (protocol-level only, not SQL `PREPARE`) |
| RLS via session variables | Session state | `SET LOCAL`, or session mode for that path |

## PgBouncer setup notes

```ini
pool_mode = transaction
max_client_conn = 5000
default_pool_size = 20        ; per user/db pair; total must fit max_connections minus admin headroom
max_db_connections = 50
max_prepared_statements = 100 ; 1.21+
query_wait_timeout = 30
server_lifetime = 3600        ; recycle backends whose caches grew
```

- PgBouncer is single-threaded. When one core is pegged, run several processes
  on one port with `so_reuseport`, and use `peers` so cancel requests find the
  right process.
- Monitor `SHOW POOLS`. A rising `cl_waiting` explains latency that
  `pg_stat_statements` never sees.
- Use `PAUSE`/`RESUME` for planned cutovers.
- Auth: use `auth_query` with a `SECURITY DEFINER` lookup function over
  `userlist.txt` sprawl, and SCRAM.

## Timeouts and failure behavior

- Set `idle_in_transaction_session_timeout`, plus a per-role
  `statement_timeout`.
- Use TCP keepalives to detect dead peers. Linux defaults can hide 15-minute
  stalls behind short app timeouts.
- `client_connection_check_interval` stops queries whose client has gone.
- `idle_session_timeout` can kill healthy pooled connections. Use it only to
  reap backends behind a pooler, or not at all.
- Add jitter to reconnect loops and fleet restarts, to avoid retry storms.
- Don't fail all replica reads over to the primary at once.
- Use multi-host libpq connection strings (`target_session_attrs`) so clients
  find the new primary after failover.

## Alternatives

- **PgCat** and **PgDog**: multi-threaded, with load balancing. PgDog adds
  sharding and query routing.
- **Supavisor**: multi-tenant, with million-connection tests.
- **RDS Proxy**: pins connections on session state.
- Vendor benchmarks disagree. Test with your own concurrency and query mix.
