# Capsule backup implementation boundary

The schema-v8 implementation supplies the device-side, locally verifiable part of
the encrypted backup architecture:

- cloud backup remains disabled by default;
- `default` and `local:*` accounts cannot enable or create backup;
- callers must explicitly authorize one or more of `preferences`,
  `development_habits`, `schedule_habits`, and `important`;
- the user-designated important mark travels under its own `important` scope. The
  mark is the reserved plain tag `important` on a MEM-L3 record
  (`IMPORTANT_MEMORY_TAG` in `crates/memory-store/src/product.rs`), deliberately
  not a `slot:` tag: `validate_tags` allows at most one slot per record, so a
  slot would collide with the record own field slot. It is its own scope rather
  than a `kind` because the mark is orthogonal to the content category, and it
  cannot ride along with `preferences` because that scope only matches
  `kind == "preference"`: a marked `fact` would otherwise match no scope at all
  and be dropped without a word;
- L2 conversation summaries are never uploaded: `summaries` is not a valid scope
  and the payload always carries an empty `session_summaries` array (MEM-L4-02);
- raw Session Events, profile/PII, `sensitive`/`secret` memories, credentials, and
  `local_only` memories are excluded;
- selected records, current revision evidence, and the Tombstone ledger are placed
  in a versioned JSON payload, compressed with gzip and encrypted/authenticated with
  AES-256-GCM;
- `backup.list` is namespace filtered, `backup.preview_restore` verifies the whole
  Capsule, and `backup.restore` requires a daemon-verified confirmation token;
- restore applies local and Capsule Tombstones before records, never overwrites the
  database file, writes new local revisions, and rebuilds FTS from canonical rows.
- `backup.create` is idempotent: replaying the same key and parameters returns the
  original receipt and does not create a second Capsule;
- `backup.run_scheduled` evaluates `manual/daily/weekly/monthly` policy in the Server.
  Runtime invokes this check on a best-effort basis after a committed turn, so a
  disabled/default account is skipped and a not-yet-due policy does no work.
- `memoryd` also runs a bounded periodic worker independent of conversation traffic.
  It executes retention maintenance and backup due scans, stores the last result and
  exponential retry deadline in `background_worker_state`, and resumes the schedule
  after a daemon restart.

The local-directory adapter is enabled only when `memoryd` receives both:

```text
--backup-directory /protected/capsule-directory
--capsule-key-file /protected/32-byte-key
```

The key file accepts 32 raw bytes, 64 hexadecimal characters, or base64 encoding
of 32 bytes and must have mode `0600`. Production must replace this file with the
Security Secret Broker/key-envelope contract. The HTTP adapter supplies the cloud
upload/query/download transport; OAHU2 account authentication and Secret Broker
key recovery remain external contracts.
The Server-side period calculation, memoryd periodic worker and persisted retry
state are implemented locally. This
capability is ready for API-Key endpoint integration, but not a complete
two-device product qualification.

The implementation follows the project research conclusion: borrow per-scope key
separation and encrypted export/import ideas from OpenAI Agents SDK/OpenMemory, but
retain Bianbu canonical revision, identity, audit, and Tombstone semantics instead of
using an upstream dump file as an authority database.

## The user-designated important mark

`important` is a reserved plain tag on a MEM-L3 record. Only an explicit user or
agent action sets it: it is never inferred from record text, because the mark is
what decides whether the record may leave the device. Setting it is an ordinary
`long_term.revise`, so it rides the existing revision, Tombstone and Capsule
paths and needs no new wire format - `tags` already travels inside the Capsule.

Entry points:

~~~text
bianbu-agent-memory mark-important <memory_id>
bianbu-agent-memory unmark-important <memory_id>
client.long_term.mark_important(record) / unmark_important(record)   # SDK
memory(target=..., action="mark_important", memory_id=...)           # MCP tool
~~~

Marking is a no-op when the record already carries the requested state, so it
does not burn a revision. Widening the scope stays a separate decision: the
`important` entry in `backup_scopes` is what authorizes the upload, and the mark
on its own uploads nothing. On upgrade the migration appends `important` only to
a device that still holds an untouched shipped default; a scope list chosen
deliberately is left alone (`migrations/0013_add_important_backup_scope.sql`).

The mark is readable back: `memory_retrieve` returns each entry's stored `tags`
when they are non-empty, so the marked records can be read off a recall instead of
being guessed from the text.

## HTTP cloud adapter (API-Key)

The daemon now provides an optional HTTP implementation of the same
BackupStorageAdapter contract. It uploads the already encrypted Capsule to:

~~~text
POST {base_url}/v1/agent-memory/files
GET  {object_ref}
DELETE {object_ref}
~~~

Requests carry X-API-Key and the Capsule metadata headers
X-Agent-Memory-Backup-Id, X-Agent-Memory-Capsule-Version, and
X-Agent-Memory-Object-SHA256. Authenticated uploads also carry hashed account,
Agent, device and memory-space identifiers for server-side namespace indexing.
The request body is opaque encrypted bytes; the cloud service must not need the
Capsule key. Upload responses may return
backup_id, object_ref and object_size at the top level or under a data field.
If object_ref is omitted, the adapter uses
{base_url}/v1/agent-memory/files/{backup_id}.

Configure the daemon with an HTTPS endpoint and an API-Key file (the file may
be mode 0640, owned by root and group bianbu-agent):

~~~text
BIANBU_MEMORY_BACKUP_BASE_URL=https://memory.example.invalid
BIANBU_MEMORY_BACKUP_API_KEY_FILE=/etc/bianbu-agent-memory/backup-api-key
~~~

Put these lines in /etc/bianbu-agent-memory/backup.env, then restart the
bianbu-agent-memoryd.service unit. The alternative
BIANBU_MEMORY_BACKUP_API_KEY environment variable is supported for development,
but the file form is preferred. The daemon still requires a capsule key file;
the API-Key authenticates the cloud API and is never used as the Capsule key.
HTTP is rejected for non-loopback endpoints, so production deployments must
use HTTPS. curl is a runtime package dependency.

Without a cloud URL and API-Key, the packaged service continues to use its
existing local-directory adapter. Cloud backup remains opt-in and is still
subject to account identity, privacy scopes, Capsule integrity checks and the
existing confirmation-token restore boundary. Secret Broker integration and
two-device qualification remain external deployment work.
