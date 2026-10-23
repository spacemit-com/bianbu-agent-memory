# Host adapters

These integrations install external memory providers or plugins. They do not
require changes to Hermes or DeepSeek Harness source.

| Capability | Hermes v0.21.7 | DeepSeek Harness 0.1.0-rc.6 |
| --- | --- | --- |
| Explicit save, structured search, revision-checked update | Supported | Supported |
| Record deletion with a separate user confirmation | Supported | Supported |
| Automatic completed-turn ingestion and cross-session recall | Supported | Supported |
| Relative-date normalization on save and update | Supported | Supported |
| Effective Instruction Memory | Loaded at provider initialization | Read on each model step |
| Host context compression | Pre-compression boundary and message hashes | Successful host summary, event pointers and checkpoint |
| Immediate cancellation audit | No dedicated callback in the pinned provider API | Aborted turn event and recovery checkpoint |
| Complete host context snapshot | Not provided | Not provided |

Supported does not mean every deployment mode has been validated. The current
development launchers use isolated development identities. Production Runtime
registration and confirmation secrets require separate provisioning and tests.
See the individual adapter READMEs for those boundaries.

Acceptance distinguishes real model conversations, lifecycle events verified
against memory storage, and deterministic negative tests. An assistant saying
"saved" is not evidence of persistence. Cancellation tests must verify the
interrupted event, not just the host's stopped indicator. Failed compression
must not be counted as successful summary persistence.

The daemon exposes additional administration APIs that are not model tools.
They are tested through the SDK, rather than requiring every host to expose
every memory API in chat. A missing host callback is a documented host limit;
an event that the adapter promises to handle but fails to persist is a defect.

- [Hermes](hermes/README.md)
- [DeepSeek Harness](deepseek-harness/README.md)
