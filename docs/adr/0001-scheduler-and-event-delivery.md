# ADR-0001: Probe scheduling and event delivery without a message broker

**Status:** Accepted (phase 1, in this release) · Proposed (phase 2)
**Date:** 2026-11-03
**Deciders:** Rajkumar Madhu (platform lead); platform team sign-off

## Context

LinkedEye monitors on-prem trading infrastructure for NSE/BSE/MCX broker-dealers. It must keep detecting new
failures at the moment most targets fail together: a data-centre uplink drop, a market-open storm.

The 3 Nov 2026 code review found that the current design does the opposite:

- `manage.py run_probes` runs URL checks, domain checks and 16 periodic jobs in sequence on one thread. The
  heartbeat is just one of those jobs. When every target times out (100 URLs + 50 domains + 200 TCP checks at
  10 s, 8 workers), one pass takes more than 300 s. The liveness probe then kills the pod, and on restart every
  check is due again, so the scheduler crash-loops.
- `core.events.emit()` runs every listener inline: incident creation, CMDB, automation rules, webhook and e-mail
  notifications. `EMAIL_TIMEOUT` is unset, so a silent SMTP server hangs the scheduler. An Alertmanager POST
  with 300 alerts runs 300 rule actions inside one gunicorn request.
- Single-scheduler operation is enforced only by deploy manifests. A second copy (cron `--once`, a stray
  replica) double-probes, double-notifies and races on incident creation.
- State transitions use `previous = obj.last_status` followed by an unconditional `save()`. A web "Run now" that
  races the worker announces the same outage twice.

Constraints:

- On-prem; PostgreSQL 16 is the only stateful dependency we ship (MySQL 8 for LinkedEyeWebProject).
- A team of three developers who also run the platform.
- One container image; the apps must still drop into LinkedEyeWebProject.
- No new infrastructure without a strong case.

## Decision

Keep the database as the only coordination point, and fix the problem in two phases.

**Phase 1 (this release):**

1. **Single scheduler lease.** `run_probes` takes a lease before running passes: a PostgreSQL session advisory
   lock where available, otherwise a lease row with a conditional `UPDATE`. A second instance idles and logs
   that another scheduler holds the lease. Job last-run times are persisted, so cron `--once` stops re-running
   hourly jobs.
2. **Heartbeat on its own thread**, independent of how long a pass takes. The heartbeat thread also renews
   the lease.
3. **Notifications off the caller's thread.** They are scheduled with `transaction.on_commit`, so rolled-back
   events never notify, and run on a small bounded executor; when it is full, the overflow is dropped and
   logged. They are collapsed into one digest above a per-minute threshold, and `EMAIL_TIMEOUT` defaults to 10 s.
4. **Per-pass time budget for jobs.** Jobs run most-overdue first and are deferred when a pass runs long, so
   none starves.
5. **Guarded transitions:** `UPDATE … WHERE last_status = previous`. Only the writer that changed the state emits
   the event.
6. **Incident dedup via a per-fingerprint lock row** (`IncidentKey`), taken inside the transaction that opens
   the incident. This works on MySQL too, unlike a partial unique index.
7. **Bounded in-request work:**
   - Alertmanager ingest emits at most a capped number of events per request, plus one aggregate event.
   - "Run all" in the UI probes at most 50 checks per request.

**Phase 2 (proposed; see the Pipeline v2 design doc):**

- Replace the in-process loop with N stateless workers that claim due checks from a `ProbeSchedule` table
  (`SELECT … FOR UPDATE SKIP LOCKED`).
- Replace the in-process executor with a transactional outbox (`Delivery` rows per event and consumer)
  drained by a dispatcher with per-consumer retries.

## Options Considered

### Option A: Postgres-only leases and in-process async delivery (phase 1, chosen now)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low: four small changes in `core`, two new tables |
| Cost | None: no new services |
| Scalability | One scheduler per database; fine to roughly 5,000 targets at a 60 s mean interval |
| Team familiarity | High: Django ORM and Postgres only |

**Pros:**
- Closes the crash-loop, the double scheduler and the duplicate events now.
- Runs on PostgreSQL, MySQL 8 and SQLite.
- No new moving parts.

**Cons:**
- Notifications still in the executor queue are lost if the process dies. This is at-most-once delivery
  for notifications only; incidents and events are committed rows.
- Probing still scales only vertically.

### Option B: Celery + Redis (beat + workers)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium–High: broker, beat, workers, result backend, monitoring for each |
| Cost | Redis (HA needs Sentinel) per client site; more pods to patch |
| Scalability | Horizontal workers; mature |
| Team familiarity | Medium |

**Pros:**
- Retries, routing and rate limits are built in.
- Widely known.

**Cons:**
- Adds a stateful service at every on-prem client.
- Beat is itself a singleton to protect.
- Task and transaction ordering still needs on-commit discipline.
- Doesn't fit LinkedEyeWebProject without changes there.

### Option C: Event bus (NATS JetStream / Kafka)
| Dimension | Assessment |
|-----------|------------|
| Complexity | High |
| Cost | A cluster per client; storage and ops |
| Scalability | Very high: far beyond our load |
| Team familiarity | Low |

**Pros:**
- Durable fan-out.
- Replay.

**Cons:**
- Heavy for about 80 probes/s.
- Still needs an outbox to publish atomically with database writes.
- Another product to support on-site.

### Option D: Postgres outbox + `SKIP LOCKED` workers (phase 2)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium: four tables, a dispatcher role, migration of 16 jobs |
| Cost | None beyond database load (~7M result rows/day at target: partition by day) |
| Scalability | N workers; no singleton |
| Team familiarity | High |

**Pros:**
- At-least-once delivery for every consumer.
- Per-consumer isolation and retries.
- No new services.

**Cons:**
- More code to own.
- Needs MySQL 8.0.1+ or MariaDB 10.6+ for `SKIP LOCKED`.

## Trade-off Analysis

The failure that matters is losing monitoring during a mass outage. Option A removes it with the smallest change
and no new infrastructure, which is right for a three-person team supporting many on-prem sites. Its one
weakness, losing queued notifications when the process crashes, is tolerable short-term: the Event row and any
incident are already committed and visible in the console.

Option D removes that weakness and the singleton, still without new infrastructure. It is the target design.
Options B and C solve problems we don't have at roughly 80 probes/s, and they add a stateful service at every
client.

## Consequences

**Easier:**
- Running a second `run_probes` by mistake is harmless.
- A slow SMTP server or webhook no longer stalls probing.
- A burst of 300 alerts produces one digest, not 300 pages.

**Harder:**
- Notification delivery is asynchronous. Tests must force sync mode (`LINKEDEYE_NOTIFY_ASYNC=false`), and
  "did it send" is now a log line rather than a return value.

**To revisit:**
- At-most-once notifications (phase 2 outbox).
- Vertical-only probing above roughly 5,000 targets.
- Result-table growth, via day partitions and rollups.
- The 50-check cap on "Run all", which becomes a queued run in phase 2.

## Action Items
1. [x] Lease, heartbeat thread, async notify + digest, `EMAIL_TIMEOUT`, job budget (this release)
2. [x] Guarded transitions; `IncidentKey` dedup; ingest and "Run all" caps (this release)
3. [ ] Alert rules for the scheduler: heartbeat age > 120 s, notify queue drops > 0, lease contention
4. [ ] Phase 2 design review: `ProbeSchedule` + `Delivery` outbox (Pipeline v2 design doc)
5. [ ] Partition `UrlResult` / `DomainCheck` / TCP results by day before any client passes 2,000 targets
