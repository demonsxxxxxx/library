# Ingest cancellation after source deletion

## Contract

`services.entries.soft_delete_entry(db, *, entry_id, purge_after_seconds)` and
`services.folders.soft_delete_folder(db, *, folder_id, purge_after_seconds)`
retain originals and the existing purge window. Before commit they flush entry
tombstones and call `repositories.tasks.cancel_orphan_ingests(db, *, now,
file_ids)`. A File is ingestible only while `files.deleted_at IS NULL` and at
least one `file_entries.deleted_at IS NULL` references it. Lifecycle archival
is not deletion. Removing one of several live entries must not cancel shared
file parsing.

Cancellation matches `tasks.payload.file_id`, not just dedup keys. It changes
pending/running ingest tasks to `cancelled`, sets `finished_at` and
`last_error=no_live_entry`, and clears `locked_by` and `lease_expires_at` in the
deleting transaction. Rollback restores both entries and queue state.

`lock_ingest_file(db, file_id)` takes the database writer/row lock by a no-op
File UPDATE. Deletion, enqueue/reprocess and ingest persistence serialize on
that row before changing task state. `_persist` takes the writer/row lock
before reading source state. Multi-file recovery locks file ids in sorted
order. Normal claim selection locks task rows only, filters orphan file ids,
and commits before handler-side file locking.

## Runtime boundaries

The `guard_ingest_delivery` wrapper binds a context-local File/task/delivery
owner. Runner-owned `_task_id`, `_task_attempt` and `_task_owner` override any
payload fields. `check_ingest_delivery(session=None)` validates live entries
and the exact running task owner. The optional session permits checks inside
the persistence transaction; standalone checks use a fresh session.

`IngestCancelled` subclasses asyncio.CancelledError. Broad pipeline error,
retry and failover catches must not turn it into a failed parse. The runner
records cancellation through an owner-fenced transition, with no applied/error
TaskOutcome. Its existing fenced heartbeat cancels a blocked coroutine when
the deletion transaction releases its queue ownership. With default settings,
a blocked operation observes that signal at the next 20-second heartbeat.
An already-sent upstream request cannot be undone at the provider.

Checkpoints run before pipeline execution, status/result persistence,
embedding refresh, and after model rate-limit waits. Provider HTTP request
hooks check again for SDK-internal retries. `ingest_publication_fence()` holds
the file/delivery locks while synchronous semantic index files publish.
Deleted/expired deliveries must not publish results, even if an entry has
subsequently been restored and a fresh task exists.

Enqueue/reprocess, unfiltered bulk scope queries, low-quality candidate
selection, worker startup reconciliation and stale recovery validate live
entries. Malformed payloads without file_id keep their validation/failure path.
Deduplicated reupload can create a new task for a retained File; it never
revives the old cancelled task. There is no new restore API.

## Schema and clients

Revision `0017_cancelled_ingest_tasks` and its bootstrap shim widen
`ck_tasks_status` and reconcile active legacy orphan tasks. The shim is
idempotent and preserves done/dead history and originals. SQLite rebuilds the
task table using Alembic batch operations, preserving modeled indexes.
PostgreSQL alters its CHECK through the same operation API. Apply the migration
before starting new workers when runtime schema bootstrap is disabled. Keep
API and workers on the same candidate; old binaries do not know this status.

Recent activity includes cancelled rows with a neutral icon and translated
label. They have no failure badge or retry-attempt usage detail. Throughput,
success rate and mean duration only consume done/dead; terminal retention also
includes cancelled rows.

## Required regressions

`tests/test_ingest_cancellation_unit.py` covers entry/folder pending/running,
shared entries, rollback, no subsequent model calls, blocked heartbeat
cancellation, SDK-internal retry, no original read for queued deletes, stale
persistence/publication after restore, both delete/persist lock orderings,
bulk/reprocess/enqueue exclusion, queue retry/recovery fencing, metrics and
legacy migration preservation/idempotence.

Adjacent suites: ingest_file, task_delivery_fencing, task_runner_concurrency,
bulk_reprocess, task_throughput, semantic_index/rebuild_resume,
provider_resilience, llm_retry, scale_safety, ingest_status_reconciliation,
reprocess, low_quality_reprocess, runner_no_llm_key, folders_ingest_status,
file_entry_path, dispatcher, scan_sync and WebDAV sync/conflict guards.
