use memory_core::{capability, MemoryIdentity, PROTOCOL_VERSION};
use memory_protocol::RequestEnvelope;
use memory_store::{migrate_plaintext_database, report_namespaces, Store};
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::os::unix::fs::PermissionsExt;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use tempfile::TempDir;
use uuid::Uuid;

fn identity() -> MemoryIdentity {
    MemoryIdentity {
        account_id: "local:test".to_owned(),
        device_id: "k3-test".to_owned(),
        agent_id: "io.bianbu.test".to_owned(),
        agent_name: "io.bianbu.test".to_owned(),
        agent_instance_id: "test-instance".to_owned(),
        memory_space_id: "test-space".to_owned(),
        capabilities: capability::ALL
            .iter()
            .map(|value| (*value).to_owned())
            .collect::<BTreeSet<_>>(),
        peer_uid: 1000,
        peer_gid: 1000,
        peer_pid: 42,
        peer_start_time_ticks: 1,
    }
}

fn request(method: &str, params: Value, idempotency_key: &str) -> RequestEnvelope {
    RequestEnvelope {
        protocol: PROTOCOL_VERSION.to_owned(),
        request_id: Uuid::new_v4().to_string(),
        trace_id: Uuid::new_v4().to_string(),
        method: method.to_owned(),
        params,
        idempotency_key: Some(idempotency_key.to_owned()),
        deadline_ms: Some(5000),
    }
}

/// `system.health` only takes a `PRAGMA quick_check` verdict once the database
/// has been quiet for `AGENT_MEMORY_HEALTH_SETTLE_MS` (default 3 s); a check
/// taken while writes are recent misreports FTS5 indexes on the shipped
/// SQLCipher build (docs/GATEWAY_EMBEDDING_INTEGRATION.md §8.2). Tests that
/// assert on a verdict wait that window out.
fn wait_for_settle_window() {
    let settle_ms: i64 = std::env::var("AGENT_MEMORY_HEALTH_SETTLE_MS")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(3_000);
    thread::sleep(std::time::Duration::from_millis(settle_ms as u64 + 250));
}

#[test]
fn concurrent_reads_keep_each_audit_durable_alongside_writes() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Arc::new(Store::open(&database).unwrap());
    let barrier = Arc::new(std::sync::Barrier::new(9));
    let mut workers = Vec::new();
    for worker in 0..8 {
        let store = Arc::clone(&store);
        let barrier = Arc::clone(&barrier);
        let database = database.clone();
        workers.push(thread::spawn(move || {
            barrier.wait();
            for index in 0..16 {
                let mut read = request("privacy.settings.get", json!({}), "read-audit");
                read.trace_id = format!("concurrent-read:{worker}:{index}");
                store.handle(&identity(), &read, false).unwrap();
                // A successful read must already have a durable audit, not
                // merely have queued one for a later background flush.
                let connection = Connection::open(&database).unwrap();
                let count: i64 = connection.query_row(
                    "SELECT COUNT(*) FROM audit_spool WHERE trace_id=?1 AND event_type='memory_read'",
                    [&read.trace_id], |row| row.get(0),
                ).unwrap();
                assert_eq!(count, 1);
            }
        }));
    }
    barrier.wait();
    for index in 0..16 {
        store.handle(&identity(), &request(
            "long_term.put", json!({"kind":"fact","text":format!("audit writer {index}"),"sync_policy":"local_only"}),
            &format!("audit-writer:{index}"),
        ), false).unwrap();
    }
    for worker in workers {
        worker.join().unwrap();
    }
}

#[test]
fn concurrent_writes_are_serialized_without_sqlite_busy_errors() {
    let directory = TempDir::new().unwrap();
    let store = Arc::new(Store::open(directory.path().join("memory.db")).unwrap());
    let workers = 8;
    let writes_per_worker = 8;
    let handles = (0..workers)
        .map(|worker| {
            let store = Arc::clone(&store);
            thread::spawn(move || {
                let owner = identity();
                for index in 0..writes_per_worker {
                    store
                        .handle(
                            &owner,
                            &request(
                                "long_term.put",
                                json!({
                                    "kind":"fact",
                                    "text":format!("concurrent writer {worker}:{index}"),
                                    "sync_policy":"local_only"
                                }),
                                &format!("concurrent:{worker}:{index}"),
                            ),
                            false,
                        )
                        .unwrap();
                }
            })
        })
        .collect::<Vec<_>>();
    for handle in handles {
        handle.join().unwrap();
    }
    let owner = identity();
    let stats = store
        .handle(
            &owner,
            &request("system.stats", json!({}), "concurrent:stats"),
            false,
        )
        .unwrap();
    assert_eq!(stats["long_term_memories"], workers * writes_per_worker);
}

#[test]
fn product_settings_are_file_backed_and_runtime_readable() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let config_path = directory.path().join("memory.config.json");
    let store = Store::open(&database).unwrap();
    let owner = identity();

    let defaults = store
        .handle(
            &owner,
            &request("privacy.settings.get", json!({}), "product-defaults"),
            false,
        )
        .unwrap();
    assert_eq!(defaults["identity"]["default_account_id"], "default");
    assert_eq!(defaults["identity"]["cloud_upload_allowed"], false);
    assert_eq!(defaults["recent_turn_limit"], 5);
    assert_eq!(defaults["summary_interval_turns"], 5);
    assert_eq!(defaults["summary_retention_days"], 7);
    assert_eq!(defaults["preference_auto_apply_enabled"], true);
    assert_eq!(defaults["development_habit_auto_apply_enabled"], true);
    assert!(config_path.is_file());
    assert_eq!(
        std::fs::metadata(&config_path)
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );

    let updated = store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({
                    "timezone":"UTC",
                    "local_device_id":"k3-lab-01",
                    "recent_turn_limit":8,
                    "recent_session_limit":6,
                    "summary_interval_turns":4,
                    "summary_retention_days":30,
                    "preference_auto_apply_enabled":false,
                    "development_habit_auto_apply_enabled":true
                }),
                "product-update",
            ),
            false,
        )
        .unwrap();
    assert_eq!(updated["identity"]["timezone"], "UTC");
    assert_eq!(updated["identity"]["configured_device_id"], "k3-lab-01");
    assert_eq!(updated["recent_turn_limit"], 8);
    assert_eq!(updated["recent_session_limit"], 6);
    assert_eq!(updated["summary_interval_turns"], 4);
    assert_eq!(updated["summary_retention_days"], 30);
    assert_eq!(updated["preference_auto_apply_enabled"], false);

    let persisted: Value = serde_json::from_slice(&std::fs::read(config_path).unwrap()).unwrap();
    assert_eq!(persisted["timezone"], "UTC");
    assert_eq!(persisted["recent_turn_limit"], 8);
}

#[test]
fn privacy_settings_can_update_only_recent_session_limit() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let updated = store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({"recent_session_limit": 6}),
                "recent-session-limit-only",
            ),
            false,
        )
        .unwrap();
    assert_eq!(updated["recent_session_limit"], 6);
}

#[test]
fn summary_model_rejects_https_until_the_daemon_has_a_tls_client() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let error = store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({
                    "summary_model": {
                        "enabled": true,
                        "base_url": "https://model.example/v1",
                        "model": "summary-model"
                    }
                }),
                "reject-https-summary-model",
            ),
            false,
        )
        .unwrap_err();
    assert_eq!(error.code, "InvalidArgument");
    assert!(error.message.contains("http://"));
}

#[test]
fn default_account_never_enqueues_cloud_upload_or_sync() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let mut owner = identity();
    owner.account_id = "default".to_owned();
    store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({
                    "kind":"preference",
                    "text":"默认使用中文",
                    "sync_policy":"account_agent"
                }),
                "default-local-memory",
            ),
            false,
        )
        .unwrap();
    let sync_error = store
        .handle(
            &owner,
            &request(
                "sync.request",
                json!({"scopes":["preferences"]}),
                "default-sync",
            ),
            false,
        )
        .unwrap_err();
    assert_eq!(sync_error.code, "Unauthenticated");
    let connection = Connection::open(database).unwrap();
    let pending: i64 = connection
        .query_row("SELECT COUNT(*) FROM outbox", [], |row| row.get(0))
        .unwrap();
    assert_eq!(pending, 0);
}

#[test]
fn legacy_development_account_migrates_to_default_without_data_loss() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let mut legacy = identity();
    legacy.account_id = "local:uid:1000".to_owned();
    let memory = store
        .handle(
            &legacy,
            &request(
                "long_term.put",
                json!({"kind":"fact","text":"迁移前的本地记忆"}),
                "legacy-memory",
            ),
            false,
        )
        .unwrap();
    {
        let connection = Connection::open(&database).unwrap();
        for (op_id, account_id) in [
            ("legacy-pending-upload", "local:uid:1000"),
            ("default-pending-upload", "default"),
        ] {
            connection
                .execute(
                    "INSERT INTO outbox(
                        op_id,account_id,agent_id,device_id,record_type,record_id,
                        operation,revision,hybrid_clock,payload_ciphertext,payload_hash,
                        sync_policy,created_at,status
                     ) VALUES(?1,?2,'agent','device','long_term','record','upsert',1,
                              '1:0:device',X'00','hash','account_agent',1,'pending')",
                    params![op_id, account_id],
                )
                .unwrap();
        }
        connection
            .execute(
                "INSERT INTO sync_jobs(
                    job_id,account_id,agent_id,scopes_json,state,created_at,updated_at
                 ) VALUES('legacy-sync','local:uid:1000','agent','[]','pending',1,1)",
                [],
            )
            .unwrap();
    }
    let migration = store.migrate_development_account_to_default(1000).unwrap();
    assert_eq!(migration["state"], "migrated");
    // outbox holds two rows (legacy account + default), sync_jobs one.
    assert_eq!(migration["discarded_cloud_rows"], 3);

    let mut current = legacy;
    current.account_id = "default".to_owned();
    let fetched = store
        .handle(
            &current,
            &request(
                "long_term.get_many",
                json!({"memory_ids":[memory["memory_id"].clone()]}),
                "migrated-memory-read",
            ),
            false,
        )
        .unwrap();
    assert_eq!(fetched[0]["text"], "迁移前的本地记忆");

    let connection = Connection::open(database).unwrap();
    for table in [
        "outbox",
        "inbox",
        "sync_cursors",
        "sync_conflicts",
        "sync_jobs",
    ] {
        let count: i64 = connection
            .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 0, "{table} must be empty after local migration");
    }
}

#[test]
fn background_worker_runs_maintenance_and_persists_its_schedule() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();
    store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({"retention_days":7}),
                "background-settings",
            ),
            false,
        )
        .unwrap();
    store
        .handle(
            &owner,
            &request(
                "sessions.append_event",
                json!({
                    "session_id":"background-session",
                    "event_type":"user_message",
                    "payload":{"role":"user","text":"expired background event"},
                    "expires_at":1
                }),
                "background-expired-event",
            ),
            false,
        )
        .unwrap();

    let first = store.run_background_jobs("k3-background").unwrap();
    assert!(first["attempted"].as_i64().unwrap() >= 2);
    assert_eq!(first["failed"], 0);

    let connection = Connection::open(&database).unwrap();
    let remaining: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM session_events WHERE session_id='background-session'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(remaining, 0);
    let states: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM background_worker_state
             WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
            params![owner.account_id, owner.agent_id, owner.memory_space_id],
            |row| row.get(0),
        )
        .unwrap();
    // summary + maintenance + embedding + lexical schedules are persisted per
    // namespace
    assert_eq!(states, 4);
    drop(connection);

    let second = store.run_background_jobs("k3-background").unwrap();
    assert_eq!(second["attempted"], 0);
    assert!(second["deferred"].as_i64().unwrap() >= 2);
}

#[test]
fn authoritative_memory_flow() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let mut identity = identity();
    identity.account_id = "account:test".to_owned();

    let instruction_request = request(
        "instructions.put",
        json!({
            "text":"默认使用中文回答",
            "scope":"account",
            "priority":100,
            "sync_policy":"account_agent"
        }),
        "instruction-language-v1",
    );
    let first = store
        .handle(&identity, &instruction_request, false)
        .unwrap();
    let repeated = store
        .handle(&identity, &instruction_request, false)
        .unwrap();
    assert_eq!(first["instruction_id"], repeated["instruction_id"]);

    let event = store.handle(
        &identity,
        &request(
            "sessions.append_event",
            json!({"session_id":"session-1","event_type":"user_message","payload":{"text":"hello"}}),
            "event-1",
        ),
        false,
    ).unwrap();
    assert_eq!(event["event_sequence"], 1);

    let summary = store
        .handle(
            &identity,
            &request(
                "sessions.update_summary",
                json!({
                    "session_id":"session-1",
                    "patch":{"current_status":"working","next_steps":["test revisions"]},
                    "source_event_sequence":1,
                    "expected_revision":0
                }),
                "summary-1",
            ),
            false,
        )
        .unwrap();
    assert_eq!(summary["revision"], 1);

    let memory = store
        .handle(
            &identity,
            &request(
                "long_term.put",
                json!({
                    "kind":"preference",
                    "text":"用户希望日程提醒提前十五分钟",
                    "summary":"日程提醒偏好",
                    "evidence":[{"evidence_type":"session_event","evidence_id":event["event_id"]}],
                    "sync_policy":"account_agent",
                    "sensitivity":"private"
                }),
                "memory-1",
            ),
            false,
        )
        .unwrap();
    let memory_id = memory["memory_id"].as_str().unwrap();

    let headers = store
        .handle(
            &identity,
            &request(
                "long_term.search_headers",
                json!({"query":"日程提醒","kinds":["preference"],"top_k":10}),
                "search-1",
            ),
            false,
        )
        .unwrap();
    assert_eq!(headers.as_array().unwrap().len(), 1);
    assert_eq!(headers[0]["memory_id"], memory_id);

    let revised = store.handle(
        &identity,
        &request(
            "long_term.revise",
            json!({"memory_id":memory_id,"text":"用户希望日程提醒提前三十分钟","expected_revision":1}),
            "memory-revise-1",
        ),
        false,
    ).unwrap();
    assert_eq!(revised["revision"], 2);

    let conflict = store
        .handle(
            &identity,
            &request(
                "long_term.revise",
                json!({"memory_id":memory_id,"text":"stale","expected_revision":1}),
                "memory-revise-stale",
            ),
            false,
        )
        .unwrap_err();
    assert_eq!(conflict.code, "Conflict");

    let forgotten = store
        .handle(
            &identity,
            &request(
                "long_term.forget",
                json!({"memory_id":memory_id,"confirm_token":"already-verified"}),
                "forget-1",
            ),
            true,
        )
        .unwrap();
    assert_eq!(forgotten["state"], "local_hidden");
    assert_eq!(forgotten["receipt"]["deleted_revision"], 2);

    let items = store
        .handle(
            &identity,
            &request(
                "long_term.get_many",
                json!({"memory_ids":[memory_id]}),
                "get-after-forget",
            ),
            false,
        )
        .unwrap();
    assert!(items.as_array().unwrap().is_empty());

    let status = store
        .handle(
            &identity,
            &request("sync.status", json!({}), "sync-status"),
            false,
        )
        .unwrap();
    assert!(status["pending_upload"].as_i64().unwrap() >= 4);
}

#[test]
fn capabilities_fail_closed() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let mut identity = identity();
    identity.capabilities.clear();
    let error = store
        .handle(
            &identity,
            &request("instructions.list", json!({}), "denied"),
            false,
        )
        .unwrap_err();
    assert_eq!(error.code, "PermissionDenied");
}

#[test]
fn mem0_delete_proposal_is_persisted_without_deleting_the_memory() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let identity = identity();

    let memory = store
        .handle(
            &identity,
            &request(
                "long_term.put",
                json!({
                    "kind":"preference",
                    "text":"用户喜欢浅色主题",
                    "sync_policy":"local_only"
                }),
                "mem0-delete-proposal-memory",
            ),
            false,
        )
        .unwrap();
    let memory_id = memory["memory_id"].as_str().unwrap();

    let event = store
        .handle(
            &identity,
            &request(
                "sessions.append_event",
                json!({
                    "session_id":"mem0-delete-proposal-session",
                    "event_type":"memory_delete_proposed",
                    "payload":{
                        "memory_id":memory_id,
                        "reason":"mem0_contradiction_or_delete_instruction",
                        "requires_confirm_token":true
                    }
                }),
                "mem0-delete-proposal-event",
            ),
            false,
        )
        .unwrap();
    assert_eq!(event["event_sequence"], 1);

    let context = store
        .handle(
            &identity,
            &request(
                "sessions.get_context",
                json!({
                    "session_id":"mem0-delete-proposal-session",
                    "include_events":true,
                    "include_summary":false
                }),
                "mem0-delete-proposal-context",
            ),
            false,
        )
        .unwrap();
    assert_eq!(context["events"][0]["event_type"], "memory_delete_proposed");

    let still_present = store
        .handle(
            &identity,
            &request(
                "long_term.get_many",
                json!({"memory_ids":[memory_id]}),
                "mem0-delete-proposal-memory-still-present",
            ),
            false,
        )
        .unwrap();
    assert_eq!(still_present.as_array().unwrap().len(), 1);
}

#[test]
fn dplus_projection_job_is_durable_and_resolves_to_canonical_memory() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let identity = identity();

    let user_event = store
        .handle(
            &identity,
            &request(
                "sessions.append_event",
                json!({
                    "session_id":"dplus-session",
                    "event_type":"user_message",
                    "payload":{"text":"请记住终端配色偏好是深色","run_id":"dplus-run"}
                }),
                "dplus-user-event",
            ),
            false,
        )
        .unwrap();
    let assistant_event = store
        .handle(
            &identity,
            &request(
                "sessions.append_event",
                json!({
                    "session_id":"dplus-session",
                    "event_type":"assistant_message",
                    "payload":{"text":"好的","run_id":"dplus-run"}
                }),
                "dplus-assistant-event",
            ),
            false,
        )
        .unwrap();
    let job = store
        .handle(
            &identity,
            &request(
                "projection.enqueue_turn",
                json!({
                    "provider":"mem0-oss-v2",
                    "run_id":"dplus-run",
                    "session_id":"dplus-session",
                    "user_event_id":user_event["event_id"],
                    "assistant_event_id":assistant_event["event_id"]
                }),
                "dplus-enqueue",
            ),
            false,
        )
        .unwrap();
    let claimed = store
        .handle(
            &identity,
            &request(
                "projection.claim",
                json!({"provider":"mem0-oss-v2","worker_id":"worker-1","lease_ms":30000}),
                "dplus-claim",
            ),
            false,
        )
        .unwrap();
    assert_eq!(claimed["job_id"], job["job_id"]);
    assert_eq!(claimed["user_text"], "请记住终端配色偏好是深色");

    store
        .handle(
            &identity,
            &request(
                "projection.save_result",
                json!({
                    "job_id":job["job_id"],
                    "worker_id":"worker-1",
                    "provider_result":{"results":[{"id":"mem0-1","memory":"终端配色偏好：深色","event":"ADD"}]}
                }),
                "dplus-save-result",
            ),
            false,
        )
        .unwrap();
    let canonical = store
        .handle(
            &identity,
            &request(
                "long_term.put",
                json!({
                    "kind":"preference",
                    "text":"终端配色偏好：深色",
                    "sync_policy":"local_only"
                }),
                "dplus-canonical",
            ),
            false,
        )
        .unwrap();
    store
        .handle(
            &identity,
            &request(
                "projection.complete",
                json!({
                    "job_id":job["job_id"],
                    "worker_id":"worker-1",
                    "mappings":[{
                        "provider_memory_id":"mem0-1",
                        "memory_id":canonical["memory_id"],
                        "canonical_revision":1,
                        "state":"active",
                        "last_score":0.91
                    }]
                }),
                "dplus-complete",
            ),
            false,
        )
        .unwrap();
    let resolved = store
        .handle(
            &identity,
            &request(
                "projection.resolve",
                json!({"provider":"mem0-oss-v2","provider_memory_ids":["mem0-1"]}),
                "dplus-resolve",
            ),
            false,
        )
        .unwrap();
    assert_eq!(resolved[0]["memory_id"], canonical["memory_id"]);
    assert_eq!(resolved[0]["state"], "active");
}

#[test]
fn dead_projection_jobs_can_be_inspected_and_requeued_within_the_namespace() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let identity = identity();

    let user_event = store
        .handle(
            &identity,
            &request(
                "sessions.append_event",
                json!({
                    "session_id":"dead-job-session",
                    "event_type":"user_message",
                    "payload":{"text":"请记住使用 uv","run_id":"dead-job-run"}
                }),
                "dead-job-user",
            ),
            false,
        )
        .unwrap();
    let assistant_event = store
        .handle(
            &identity,
            &request(
                "sessions.append_event",
                json!({
                    "session_id":"dead-job-session",
                    "event_type":"assistant_message",
                    "payload":{"text":"好的","run_id":"dead-job-run"}
                }),
                "dead-job-assistant",
            ),
            false,
        )
        .unwrap();
    let job = store
        .handle(
            &identity,
            &request(
                "projection.enqueue_turn",
                json!({
                    "provider":"mem0-oss-v2",
                    "run_id":"dead-job-run",
                    "session_id":"dead-job-session",
                    "user_event_id":user_event["event_id"],
                    "assistant_event_id":assistant_event["event_id"]
                }),
                "dead-job-enqueue",
            ),
            false,
        )
        .unwrap();
    store
        .handle(
            &identity,
            &request(
                "projection.claim",
                json!({"provider":"mem0-oss-v2","worker_id":"dead-worker","lease_ms":30000}),
                "dead-job-claim",
            ),
            false,
        )
        .unwrap();
    store
        .handle(
            &identity,
            &request(
                "projection.fail",
                json!({
                    "job_id":job["job_id"],
                    "worker_id":"dead-worker",
                    "error":"synthetic terminal failure",
                    "retry_after_ms":1000,
                    "terminal":true
                }),
                "dead-job-fail",
            ),
            false,
        )
        .unwrap();

    let dead = store
        .handle(
            &identity,
            &request(
                "projection.list_dead",
                json!({"provider":"mem0-oss-v2","limit":10}),
                "dead-job-list",
            ),
            false,
        )
        .unwrap();
    assert_eq!(dead.as_array().unwrap().len(), 1);
    assert_eq!(dead[0]["job_id"], job["job_id"]);
    assert_eq!(dead[0]["last_error"], "synthetic terminal failure");

    let mut other_identity = identity.clone();
    other_identity.agent_id = "io.bianbu.other".to_owned();
    let other_dead = store
        .handle(
            &other_identity,
            &request(
                "projection.list_dead",
                json!({"provider":"mem0-oss-v2","limit":10}),
                "other-dead-job-list",
            ),
            false,
        )
        .unwrap();
    assert!(other_dead.as_array().unwrap().is_empty());
    let other_requeue = store.handle(
        &other_identity,
        &request(
            "projection.requeue_dead",
            json!({"provider":"mem0-oss-v2","job_id":job["job_id"]}),
            "other-dead-job-requeue",
        ),
        false,
    );
    assert_eq!(other_requeue.unwrap_err().code, "Conflict");

    let requeued = store
        .handle(
            &identity,
            &request(
                "projection.requeue_dead",
                json!({"provider":"mem0-oss-v2","job_id":job["job_id"]}),
                "dead-job-requeue",
            ),
            false,
        )
        .unwrap();
    assert_eq!(requeued["state"], "pending");
    assert_eq!(requeued["attempt_count"], 0);
    assert_eq!(requeued["previous_error"], "synthetic terminal failure");
    assert!(store
        .handle(
            &identity,
            &request(
                "projection.list_dead",
                json!({"provider":"mem0-oss-v2"}),
                "dead-job-list-after-requeue",
            ),
            false,
        )
        .unwrap()
        .as_array()
        .unwrap()
        .is_empty());
    let claimed = store
        .handle(
            &identity,
            &request(
                "projection.claim",
                json!({"provider":"mem0-oss-v2","worker_id":"recovery-worker","lease_ms":30000}),
                "dead-job-recovery-claim",
            ),
            false,
        )
        .unwrap();
    assert_eq!(claimed["job_id"], job["job_id"]);
    assert_eq!(claimed["attempt_count"], 1);
}

#[test]
fn version_one_database_is_upgraded_without_losing_session_events() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let connection = Connection::open(&database).unwrap();
    connection
        .execute_batch(
            "CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at INTEGER NOT NULL
             );
             INSERT INTO schema_migrations(version, applied_at) VALUES(1, 1);
             CREATE TABLE session_events (
                event_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                memory_space_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                event_sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN (
                  'user_message','assistant_message','tool_call','tool_result',
                  'task_started','task_completed','task_failed','checkpoint_marker','summary_updated'
                )),
                payload BLOB NOT NULL,
                payload_hash TEXT NOT NULL,
                sync_policy TEXT NOT NULL CHECK(sync_policy IN ('local_only','summary_only','account_agent','full')),
                expires_at INTEGER,
                created_at INTEGER NOT NULL,
                hlc TEXT NOT NULL,
                UNIQUE(account_id, agent_id, session_id, event_sequence)
             );
             CREATE INDEX idx_session_events_context
             ON session_events(account_id, agent_id, session_id, event_sequence);",
        )
        .unwrap();
    connection
        .execute(
            "INSERT INTO session_events(
                event_id,account_id,agent_id,memory_space_id,session_id,event_sequence,
                event_type,payload,payload_hash,sync_policy,created_at,hlc
             ) VALUES(?1,?2,?3,?4,?5,1,'user_message',?6,'legacy-hash','local_only',1,'1-0-test')",
            params![
                "legacy-event",
                "local:test",
                "io.bianbu.test",
                "test-space",
                "legacy-session",
                b"{\"text\":\"legacy\"}",
            ],
        )
        .unwrap();
    drop(connection);

    let store = Store::open(&database).unwrap();
    let identity = identity();
    let health = store
        .handle(
            &identity,
            &request("system.health", json!({}), "upgrade-health"),
            false,
        )
        .unwrap();
    assert_eq!(health["schema_version"], 14);

    let event = store
        .handle(
            &identity,
            &request(
                "sessions.append_event",
                json!({
                    "session_id":"legacy-session",
                    "event_type":"memory_delete_proposed",
                    "payload":{"memory_id":"candidate","requires_confirm_token":true}
                }),
                "upgrade-delete-proposal",
            ),
            false,
        )
        .unwrap();
    assert_eq!(event["event_sequence"], 2);

    let context = store
        .handle(
            &identity,
            &request(
                "sessions.get_context",
                json!({
                    "session_id":"legacy-session",
                    "include_events":true,
                    "include_summary":false
                }),
                "upgrade-context",
            ),
            false,
        )
        .unwrap();
    assert_eq!(context["events"].as_array().unwrap().len(), 2);
    assert_eq!(context["events"][0]["event_type"], "user_message");
    assert_eq!(context["events"][1]["event_type"], "memory_delete_proposed");
}

#[test]
fn version_five_database_hardens_legacy_profile_projection() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let connection = Connection::open(&database).unwrap();
    connection
        .execute_batch(concat!(
            include_str!("../../../migrations/0001_init.sql"),
            include_str!("../../../migrations/0002_add_memory_delete_proposed_event.sql"),
            include_str!("../../../migrations/0003_add_semantic_projection.sql"),
            include_str!("../../../migrations/0004_add_recent_session_context_indexes.sql"),
            include_str!("../../../migrations/0005_add_local_management.sql"),
            "INSERT INTO schema_migrations(version, applied_at) VALUES
                (1,1),(2,2),(3,3),(4,4),(5,5);"
        ))
        .unwrap();
    connection
        .execute(
            "INSERT INTO long_term_memories(
                memory_id,account_id,agent_id,memory_space_id,kind,content,summary,
                tags_json,source,confidence,sensitivity,current_revision,sync_policy,
                expires_at,created_at,updated_at,hlc
             ) VALUES(
                'legacy-profile','local:test','io.bianbu.test','test-space','profile',
                'Legacy User',NULL,'[\"slot:profile.user_name\"]','agent',1.0,
                'private',1,'local_only',NULL,1,1,'1-0-test'
             )",
            [],
        )
        .unwrap();
    connection
        .execute(
            "INSERT INTO semantic_projection_mappings(
                provider,account_id,agent_id,memory_space_id,provider_memory_id,
                memory_id,canonical_revision,state,created_at,updated_at
             ) VALUES(
                'mem0-oss-v2','local:test','io.bianbu.test','test-space','provider-profile',
                'legacy-profile',1,'active',1,1
             )",
            [],
        )
        .unwrap();
    drop(connection);

    Store::open(&database).unwrap();
    let connection = Connection::open(&database).unwrap();
    let schema: i64 = connection
        .query_row("SELECT MAX(version) FROM schema_migrations", [], |row| {
            row.get(0)
        })
        .unwrap();
    let sensitivity: String = connection
        .query_row(
            "SELECT sensitivity FROM long_term_memories WHERE memory_id='legacy-profile'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let mapping_state: String = connection
        .query_row(
            "SELECT state FROM semantic_projection_mappings
             WHERE provider_memory_id='provider-profile'",
            [],
            |row| row.get(0),
        )
        .unwrap();

    assert_eq!(schema, 14);
    assert_eq!(sensitivity, "sensitive");
    assert_eq!(mapping_state, "delete_proposed");
}

#[test]
fn natural_language_questions_use_lexical_recall_fallback() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let identity = identity();
    let receipt = store
        .handle(
            &identity,
            &request(
                "long_term.put",
                json!({
                    "kind":"preference",
                    "text":"终端配色偏好：浅色",
                    "sync_policy":"local_only"
                }),
                "natural-query-memory",
            ),
            false,
        )
        .unwrap();
    let memory_id = receipt["memory_id"].as_str().unwrap();

    let headers = store
        .handle(
            &identity,
            &request(
                "long_term.search_headers",
                json!({
                    "query":"你好，请问终端配色偏好是什么颜色",
                    "top_k":5
                }),
                "natural-query-search",
            ),
            false,
        )
        .unwrap();
    assert_eq!(headers[0]["memory_id"], memory_id);

    store
        .handle(
            &identity,
            &request(
                "long_term.put",
                json!({
                    "kind":"preference",
                    "text":"终端字体偏好：等宽字体",
                    "sync_policy":"local_only"
                }),
                "unrelated-terminal-preference",
            ),
            false,
        )
        .unwrap();

    let reordered = store
        .handle(
            &identity,
            &request(
                "long_term.search_headers",
                json!({
                    "query":"你好，目前终端偏好颜色是什么？",
                    "top_k":5
                }),
                "reordered-natural-query-search",
            ),
            false,
        )
        .unwrap();
    assert_eq!(reordered.as_array().unwrap().len(), 1);
    assert_eq!(reordered[0]["memory_id"], memory_id);

    let system_wording = store
        .handle(
            &identity,
            &request(
                "long_term.search_headers",
                json!({
                    "query":"请问目前系统偏好颜色是什么",
                    "top_k":5
                }),
                "system-color-preference-search",
            ),
            false,
        )
        .unwrap();
    assert_eq!(system_wording.as_array().unwrap().len(), 1);
    assert_eq!(system_wording[0]["memory_id"], memory_id);
}

// MEM-NFR-01-01: the bigram index serves the recall contract the trigram
// tokenizer could not — two-character CJK terms — through indexed lookups
// instead of the historical full-namespace LIKE scans.

fn fts2_row_count(database: &std::path::Path, memory_id: &str) -> i64 {
    let connection = Connection::open(database).unwrap();
    connection
        .query_row(
            "SELECT COUNT(*) FROM long_term_fts2 WHERE memory_id=?1",
            [memory_id],
            |row| row.get(0),
        )
        .unwrap()
}

fn fts2_terms(database: &std::path::Path, memory_id: &str) -> String {
    let connection = Connection::open(database).unwrap();
    connection
        .query_row(
            "SELECT terms FROM long_term_fts2 WHERE memory_id=?1",
            [memory_id],
            |row| row.get(0),
        )
        .unwrap()
}

#[test]
fn bigram_index_serves_two_char_cjk_queries() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let terminal = put_memory(
        &store,
        &owner,
        "终端配色偏好：浅色主题",
        "bigram-terminal",
    );
    let browser = put_memory(
        &store,
        &owner,
        "浏览器默认搜索引擎是必应",
        "bigram-browser",
    );

    // Two 2-char CJK runs: invisible to the trigram tokenizer, previously
    // only reachable through the overlap/LIKE full scans.
    let hits = search(&store, &owner, "终端 配色", "bigram-search-terminal");
    assert_eq!(hits.len(), 1);
    assert_eq!(hits[0]["memory_id"], terminal["memory_id"]);
    assert_eq!(hits[0]["score"], json!(1.0));

    let browser_hits = search(&store, &owner, "浏览器 默认", "bigram-search-browser");
    assert_eq!(browser_hits.len(), 1);
    assert_eq!(browser_hits[0]["memory_id"], browser["memory_id"]);

    // A bare 2-char term previously fell through to the LIKE backfill.
    let single = search(&store, &owner, "配色", "bigram-search-single");
    assert_eq!(single.len(), 1);
    assert_eq!(single[0]["memory_id"], terminal["memory_id"]);
}

#[test]
fn bigram_index_is_maintained_across_put_revise_forget_and_respects_secrets() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();
    let memory = put_memory(
        &store,
        &owner,
        "构建命令使用 cargo build --release",
        "bigram-maintenance-put",
    );
    let memory_id = memory["memory_id"].as_str().unwrap();
    assert_eq!(fts2_row_count(&database, memory_id), 1);
    let terms = fts2_terms(&database, memory_id);
    assert!(terms.contains("构建"));
    assert!(terms.contains("命令"));
    assert!(terms.contains("cargo"));
    assert!(terms.contains("release"));

    store
        .handle(
            &owner,
            &request(
                "long_term.revise",
                json!({
                    "memory_id":memory_id,
                    "text":"部署命令改为 cargo install",
                    "expected_revision":1
                }),
                "bigram-maintenance-revise",
            ),
            false,
        )
        .unwrap();
    assert_eq!(fts2_row_count(&database, memory_id), 1);
    let revised = fts2_terms(&database, memory_id);
    assert!(revised.contains("部署"));
    assert!(!revised.contains("构建"), "stale terms must not survive a revise");

    // Secret rows never enter any search index, mirroring the FTS policy.
    let secret = store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({
                    "kind":"fact",
                    "text":"部署密钥存放在保险库",
                    "sensitivity":"secret",
                    "sync_policy":"local_only"
                }),
                "bigram-maintenance-secret",
            ),
            false,
        )
        .unwrap();
    assert_eq!(fts2_row_count(&database, secret["memory_id"].as_str().unwrap()), 0);
    // The query terms exist only on the secret row; ranked matching keeps
    // single-term hits from unrelated rows, so the probe must stay empty.
    let secret_hits = search(&store, &owner, "密钥 保险库", "bigram-search-secret");
    assert!(secret_hits.is_empty());

    store
        .handle(
            &owner,
            &request(
                "long_term.forget",
                json!({
                    "memory_id":memory_id,
                    "confirm_token":"verified",
                    "reason_code":"test"
                }),
                "bigram-maintenance-forget",
            ),
            true,
        )
        .unwrap();
    assert_eq!(fts2_row_count(&database, memory_id), 0);
}

#[test]
fn bigram_recall_excludes_expired_rows_and_other_namespaces() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({
                    "kind":"preference",
                    "text":"终端配色偏好：深色",
                    "expires_at":1,
                    "sync_policy":"local_only"
                }),
                "bigram-expired-put",
            ),
            false,
        )
        .unwrap();
    let mut other = identity();
    other.memory_space_id = "other-space".to_owned();
    put_memory(&store, &other, "浏览器默认首页是终端配色指南", "bigram-other-space");

    let hits = search(&store, &owner, "终端 配色", "bigram-search-expired");
    assert!(
        hits.is_empty(),
        "expired rows and foreign namespaces must stay out of recall"
    );
}

#[test]
fn cross_run_adjacency_ranks_below_true_substring_matches() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let exact = put_memory(&store, &owner, "存储层架构说明文档", "bigram-adjacency-exact");
    let adjacent = put_memory(
        &store,
        &owner,
        "先看存储，储层设计在后半部分",
        "bigram-adjacency-cross-run",
    );
    let separated = put_memory(
        &store,
        &owner,
        "存储空间不足，需要新的储层设计",
        "bigram-adjacency-separated",
    );

    let hits = search(&store, &owner, "存储层", "bigram-search-adjacency");
    assert!(!hits.is_empty());
    assert_eq!(hits[0]["memory_id"], exact["memory_id"]);
    assert_eq!(hits[0]["score"], json!(1.0));
    // The cross-run row bigram-matches ("存储，储层" tokenizes to the same
    // adjacent bigrams) but fails the substring verify: it may surface with
    // a zero score, never above the true match.
    if let Some(position) = hits
        .iter()
        .position(|hit| hit["memory_id"] == adjacent["memory_id"])
    {
        assert!(position > 0);
        assert_eq!(hits[position]["score"], json!(0.0));
    }
    // Non-adjacent bigrams never become candidates at all.
    assert!(hits
        .iter()
        .all(|hit| hit["memory_id"] != separated["memory_id"]));
}

// MEM-NFR-01-01 regression: a natural-language question is typically a
// single long CJK run (Chinese has no spaces). Treating that run as one
// giant phrase required the whole question to appear verbatim and dropped
// goldset recall from 0.66 to 0.30. Long runs now expand to their sliding
// trigram windows, OR-joined like the historical fuzzy stage: a memory
// sharing any three-character window is a candidate, ranked by window
// coverage.
#[test]
fn long_cjk_run_queries_match_by_trigram_windows() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let toll = put_memory(
        &store,
        &owner,
        "自驾出行：京藏高速的过路费从 ETC 卡自动扣款，不用停车",
        "long-run-toll",
    );
    let subway = put_memory(
        &store,
        &owner,
        "城市通勤：地铁月卡自动续费，费用从工资卡扣",
        "long-run-subway",
    );

    let hits = search(
        &store,
        &owner,
        "上高速的过路费从哪张卡里扣",
        "long-run-search",
    );
    let position = hits
        .iter()
        .position(|hit| hit["memory_id"] == toll["memory_id"])
        .expect("toll memory not recalled for the natural-language query");
    assert_eq!(position, 0);
    assert!(hits[position]["score"].as_f64().unwrap() > 0.0);
    // The subway memory shares no three-character window with the query
    // (only the two-character term 卡), so it never becomes a candidate.
    assert!(hits
        .iter()
        .all(|hit| hit["memory_id"] != subway["memory_id"]));
}

// MEM-NFR-01-01 regression (100k acceptance corpus): OR-merging the query
// runs into one MATCH let a dense run's newest-first LIMIT crowd the rare
// run's matches out of the candidate set — "docs-4074" over 100k rows
// returned 0/10 rows containing 4074. Each run now gets its own statement
// and its own slice of the fetch budget, so the rare match is always a
// candidate and its higher coverage score ranks it first.
#[test]
fn rare_ascii_run_matches_survive_dense_run_crowding() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let rare = put_memory(
        &store,
        &owner,
        "dense rare marker docs-9999 reference",
        "crowding-rare",
    );
    for index in 0..101 {
        put_memory(
            &store,
            &owner,
            &format!("dense common text {index}"),
            &format!("crowding-dense-{index}"),
        );
    }

    let hits = search(
        &store,
        &owner,
        "dense docs-9999",
        "crowding-search",
    );
    let position = hits
        .iter()
        .position(|hit| hit["memory_id"] == rare["memory_id"])
        .expect("rare docs-9999 memory lost among dense-run matches");
    assert_eq!(position, 0);
    assert_eq!(hits[position]["score"].as_f64().unwrap(), 1.0);
}

#[test]
fn lexical_backfill_upgrades_legacy_namespaces_behind_the_fallback() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();
    let terminal = put_memory(
        &store,
        &owner,
        "终端配色偏好：浅色",
        "backfill-terminal",
    );
    put_memory(
        &store,
        &owner,
        "booking confirmed for the cargo flight",
        "backfill-booking",
    );
    let ok_row = put_memory(
        &store,
        &owner,
        "ok to proceed with the release",
        "backfill-ok",
    );

    // Simulate a namespace that predates schema 12: rows exist, the bigram
    // index is empty, and the state row says the backfill has not run.
    {
        let connection = Connection::open(&database).unwrap();
        connection.execute("DELETE FROM long_term_fts2", []).unwrap();
        connection
            .execute(
                "INSERT INTO lexical_index_state(
                    account_id,agent_id,memory_space_id,ready,backfill_cursor
                 ) VALUES(?1,?2,?3,0,0)",
                params![owner.account_id, owner.agent_id, owner.memory_space_id],
            )
            .unwrap();
    }

    // While the namespace waits, searches keep working through the
    // historical SQL cascade...
    let legacy = search(&store, &owner, "终端 配色", "backfill-search-legacy");
    assert!(legacy
        .iter()
        .any(|hit| hit["memory_id"] == terminal["memory_id"]));
    // ...including its substring-LIKE semantics for 2-char ASCII runs.
    let legacy_ok = search(&store, &owner, "ok", "backfill-search-legacy-ok");
    assert_eq!(legacy_ok.len(), 2, "LIKE '%ok%' matches 'booking' too");

    let jobs = store.run_background_jobs("k3-upgrade").unwrap();
    assert_eq!(jobs["failed"], 0);
    let lexical = jobs["results"]
        .as_array()
        .unwrap()
        .iter()
        .find(|item| item["job_kind"] == "lexical")
        .expect("the lexical job must run")
        .clone();
    assert_eq!(lexical["state"], "success");
    assert_eq!(lexical["result"]["indexed"], 3);
    assert_eq!(lexical["result"]["ready"], true);

    {
        let connection = Connection::open(&database).unwrap();
        let (ready, rows): (i64, i64) = connection
            .query_row(
                "SELECT ready,
                        (SELECT COUNT(*) FROM long_term_fts2 f
                         JOIN long_term_memories l ON l.rowid=f.rowid
                         WHERE l.account_id=?1 AND l.agent_id=?2 AND l.memory_space_id=?3)
                 FROM lexical_index_state
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![owner.account_id, owner.agent_id, owner.memory_space_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(ready, 1);
        assert_eq!(rows, 3, "every non-secret row must be indexed");
    }

    // Indexed semantics: 2-char ASCII runs match whole words exactly.
    let indexed_ok = search(&store, &owner, "ok", "backfill-search-indexed-ok");
    assert_eq!(indexed_ok.len(), 1);
    assert_eq!(indexed_ok[0]["memory_id"], ok_row["memory_id"]);

    let indexed = search(&store, &owner, "终端 配色", "backfill-search-indexed");
    assert!(!indexed.is_empty());
    assert_eq!(indexed[0]["memory_id"], terminal["memory_id"]);
}

#[test]
fn namespace_and_identity_override_are_isolated() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let mut other = identity();
    other.agent_id = "io.bianbu.other".to_owned();
    other.agent_instance_id = "other-instance".to_owned();

    let receipt = store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({"kind":"fact","text":"owner only","sync_policy":"local_only"}),
                "owner-memory",
            ),
            false,
        )
        .unwrap();
    let memory_id = receipt["memory_id"].as_str().unwrap();
    let other_view = store
        .handle(
            &other,
            &request(
                "long_term.get_many",
                json!({"memory_ids":[memory_id]}),
                "other-read",
            ),
            false,
        )
        .unwrap();
    assert!(other_view.as_array().unwrap().is_empty());

    let forged = store
        .handle(
            &other,
            &request(
                "long_term.get_many",
                json!({"memory_ids":[memory_id],"agent_id":owner.agent_id}),
                "forged-read",
            ),
            false,
        )
        .unwrap_err();
    assert_eq!(forged.code, "PermissionDenied");

    store
        .handle(
            &owner,
            &request(
                "sessions.append_event",
                json!({
                    "session_id":"shared-session-id",
                    "event_type":"user_message",
                    "payload":{"run_id":"owner-run","text":"owner session only","status":"completed"}
                }),
                "owner-session-event",
            ),
            false,
        )
        .unwrap();
    let mut other_space = owner.clone();
    other_space.memory_space_id = "other-space".to_owned();
    let isolated_context = store
        .handle(
            &other_space,
            &request(
                "sessions.get_context",
                json!({
                    "session_id":"shared-session-id",
                    "include_events":true,
                    "include_summary":true
                }),
                "other-space-session-read",
            ),
            false,
        )
        .unwrap();
    assert!(isolated_context["events"].as_array().unwrap().is_empty());
    assert!(isolated_context["summary"].is_null());
    assert_eq!(isolated_context["last_event_sequence"], 0);
}

#[test]
fn recent_context_crosses_sessions_but_stays_in_identity_namespace() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();

    for (key, session_id, event_type, run_id, text) in [
        (
            "recent-1",
            "session-old",
            "user_message",
            "turn-1",
            "我是小圆",
        ),
        (
            "recent-2",
            "session-old",
            "assistant_message",
            "turn-1",
            "你好，小圆",
        ),
        (
            "recent-3",
            "session-old",
            "user_message",
            "turn-2",
            "明天九点提醒我完成工程对接",
        ),
        (
            "recent-4",
            "session-old",
            "assistant_message",
            "turn-2",
            "好的",
        ),
        (
            "recent-current",
            "session-new",
            "user_message",
            "turn-current",
            "当前会话不应重复注入",
        ),
        (
            "recent-incomplete",
            "session-old",
            "user_message",
            "turn-incomplete",
            "没有助手回答的半轮不得注入",
        ),
        (
            "recent-failed",
            "session-old",
            "assistant_message",
            "turn-failed",
            "失败回答不得注入",
        ),
    ] {
        store
            .handle(
                &owner,
                &request(
                    "sessions.append_event",
                    json!({
                        "session_id":session_id,
                        "event_type":event_type,
                        "payload":{"text":text,"run_id":run_id,
                            "status":if key == "recent-failed" {"failed"} else {"completed"}}
                    }),
                    key,
                ),
                false,
            )
            .unwrap();
    }
    store
        .handle(
            &owner,
            &request(
                "sessions.update_summary",
                json!({
                    "session_id":"session-old",
                    "patch":{"current_status":"更早对话的滚动摘要"},
                    "source_event_sequence":2,
                    "expected_revision":0
                }),
                "recent-summary",
            ),
            false,
        )
        .unwrap();

    let context = store
        .handle(
            &owner,
            &request(
                "sessions.get_recent_context",
                json!({
                    "exclude_session_id":"session-new",
                    "turn_limit":5,
                    "summary_limit":3
                }),
                "recent-context",
            ),
            false,
        )
        .unwrap();

    let messages = context["messages"].as_array().unwrap();
    assert_eq!(messages.len(), 4);
    assert_eq!(messages[0]["payload"]["text"], "我是小圆");
    assert_eq!(messages[2]["payload"]["text"], "明天九点提醒我完成工程对接");
    assert!(messages
        .iter()
        .all(|item| item["session_id"] == "session-old"));
    assert_eq!(
        context["summaries"][0]["summary"]["current_status"],
        "更早对话的滚动摘要"
    );

    let mut other = identity();
    other.agent_id = "io.bianbu.other".to_owned();
    other.agent_instance_id = "other-instance".to_owned();
    let other_context = store
        .handle(
            &other,
            &request(
                "sessions.get_recent_context",
                json!({"turn_limit":5,"summary_limit":3}),
                "recent-context-other",
            ),
            false,
        )
        .unwrap();
    assert!(other_context["messages"].as_array().unwrap().is_empty());
    assert!(other_context["summaries"].as_array().unwrap().is_empty());
}

#[test]
fn recent_context_requires_matching_run_id_and_successful_status() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    for (key, event_type, run_id, status, text) in [
        (
            "complete-user",
            "user_message",
            "ok",
            "completed",
            "保留的问题",
        ),
        (
            "complete-assistant",
            "assistant_message",
            "ok",
            "completed",
            "保留的回答",
        ),
        (
            "orphan-user",
            "user_message",
            "orphan",
            "completed",
            "孤立问题",
        ),
        (
            "mismatched-assistant",
            "assistant_message",
            "other",
            "completed",
            "错配回答",
        ),
        (
            "failed-user",
            "user_message",
            "failed",
            "failed",
            "失败问题",
        ),
        (
            "failed-assistant",
            "assistant_message",
            "failed",
            "failed",
            "失败回答",
        ),
    ] {
        store
            .handle(
                &owner,
                &request(
                    "sessions.append_event",
                    json!({
                        "session_id":"strict-session",
                        "event_type":event_type,
                        "payload":{"run_id":run_id,"status":status,"text":text}
                    }),
                    key,
                ),
                false,
            )
            .unwrap();
    }
    let context = store
        .handle(
            &owner,
            &request(
                "sessions.get_recent_context",
                json!({"turn_limit":5,"summary_limit":0}),
                "strict-context",
            ),
            false,
        )
        .unwrap();
    let messages = context["messages"].as_array().unwrap();
    assert_eq!(messages.len(), 2);
    assert_eq!(messages[0]["payload"]["text"], "保留的问题");
    assert_eq!(messages[1]["payload"]["text"], "保留的回答");
}

#[test]
fn recent_sessions_mode_groups_latest_five_sessions_and_history_paginates() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    for index in 0..6 {
        let session_id = format!("session-{index}");
        for (event_type, role) in [("user_message", "user"), ("assistant_message", "assistant")] {
            store
                .handle(
                    &owner,
                    &request(
                        "sessions.append_event",
                        json!({
                            "session_id": session_id,
                            "event_type": event_type,
                            "payload": {"run_id":format!("run-{index}"),"role":role,"status":"completed","text":format!("text-{index}-{role}")}
                        }),
                        &format!("recent-session-{index}-{role}"),
                    ),
                    false,
                )
                .unwrap();
        }
    }
    let context = store
        .handle(
            &owner,
            &request(
                "sessions.get_recent_context",
                json!({"selection_mode":"recent_sessions","session_limit":5,"event_limit_per_session":10}),
                "recent-sessions-grouped",
            ),
            false,
        )
        .unwrap();
    let sessions = context["sessions"].as_array().unwrap();
    assert_eq!(sessions.len(), 5);
    assert!(sessions
        .iter()
        .all(|item| item["events"].as_array().unwrap().len() == 2));
    let history = store
        .handle(
            &owner,
            &request(
                "sessions.get_history",
                json!({"session_id":"session-5","limit":1}),
                "history-page",
            ),
            false,
        )
        .unwrap();
    assert_eq!(history["events"].as_array().unwrap().len(), 1);
    assert!(history["next_before_sequence"].is_number());
}

#[test]
fn session_status_mode_lists_sessions_without_completed_turn_gate() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    for (key, session_id, event_type, run_id, status, text) in [
        ("status-1", "travel-session", "user_message", "turn-travel-1", "completed", "规划下周旅游行程"),
        ("status-2", "travel-session", "assistant_message", "turn-travel-1", "completed", "好的，已规划"),
        ("status-3", "travel-session", "user_message", "turn-travel-2", "completed", "改为三天两夜"),
        ("status-4", "travel-session", "assistant_message", "turn-travel-2", "completed", "已更新行程"),
        ("status-5", "travel-session", "user_message", "turn-travel-3", "completed", "这轮半途而废，之后又被新完成轮覆盖"),
        ("status-6", "travel-session", "user_message", "turn-travel-4", "completed", "再加大理"),
        ("status-7", "travel-session", "assistant_message", "turn-travel-4", "completed", "已加入行程"),
        ("status-8", "chess-session", "task_started", "run-chess", "started", "象棋游戏开发"),
        ("status-9", "chess-session", "user_message", "turn-chess-1", "completed", "帮我开发一个象棋游戏"),
        ("status-10", "broken-session", "task_started", "run-broken", "started", "尝试部署服务"),
        ("status-11", "broken-session", "task_failed", "run-broken", "failed", "部署中断的任务文本"),
    ] {
        store
            .handle(
                &owner,
                &request(
                    "sessions.append_event",
                    json!({
                        "session_id":session_id,
                        "event_type":event_type,
                        "payload":{"run_id":run_id,"status":status,"text":text}
                    }),
                    key,
                ),
                false,
            )
            .unwrap();
    }
    let context = store
        .handle(
            &owner,
            &request(
                "sessions.get_recent_context",
                json!({"selection_mode":"session_status","session_limit":5}),
                "status-context",
            ),
            false,
        )
        .unwrap();
    assert_eq!(context["selection_mode"], "session_status");
    let sessions = context["sessions"].as_array().unwrap();
    assert_eq!(sessions.len(), 3);
    assert_eq!(sessions[0]["session_id"], "broken-session");
    assert_eq!(sessions[0]["interrupted"], true);
    assert_eq!(sessions[0]["in_progress"], false);
    assert_eq!(sessions[0]["completed_turn_count"], 0);
    assert_eq!(sessions[0]["title"], "尝试部署服务");
    assert_eq!(sessions[1]["session_id"], "chess-session");
    assert_eq!(sessions[1]["in_progress"], true);
    assert_eq!(sessions[1]["interrupted"], false);
    assert_eq!(sessions[1]["completed_turn_count"], 0);
    assert_eq!(sessions[1]["title"], "帮我开发一个象棋游戏");
    assert_eq!(sessions[2]["session_id"], "travel-session");
    assert_eq!(sessions[2]["in_progress"], false);
    assert_eq!(sessions[2]["interrupted"], false);
    assert_eq!(sessions[2]["completed_turn_count"], 3);
    assert_eq!(sessions[2]["title"], "规划下周旅游行程");
    let last_active: Vec<i64> = sessions
        .iter()
        .map(|item| item["last_active_at"].as_i64().unwrap())
        .collect();
    assert!(last_active[0] >= last_active[1] && last_active[1] >= last_active[2]);

    let excluded = store
        .handle(
            &owner,
            &request(
                "sessions.get_recent_context",
                json!({
                    "selection_mode":"session_status",
                    "session_limit":5,
                    "exclude_session_id":"broken-session"
                }),
                "status-excluded",
            ),
            false,
        )
        .unwrap();
    let excluded_sessions = excluded["sessions"].as_array().unwrap();
    assert_eq!(excluded_sessions.len(), 2);
    assert!(excluded_sessions
        .iter()
        .all(|item| item["session_id"] != "broken-session"));

    let limited = store
        .handle(
            &owner,
            &request(
                "sessions.get_recent_context",
                json!({"selection_mode":"session_status","session_limit":1}),
                "status-limited",
            ),
            false,
        )
        .unwrap();
    let limited_sessions = limited["sessions"].as_array().unwrap();
    assert_eq!(limited_sessions.len(), 1);
    assert_eq!(limited_sessions[0]["session_id"], "broken-session");

    let mut other = identity();
    other.agent_id = "io.bianbu.other".to_owned();
    other.agent_instance_id = "other-instance".to_owned();
    let other_context = store
        .handle(
            &other,
            &request(
                "sessions.get_recent_context",
                json!({"selection_mode":"session_status"}),
                "status-other-identity",
            ),
            false,
        )
        .unwrap();
    assert!(other_context["sessions"].as_array().unwrap().is_empty());
}

#[test]
fn large_event_payload_is_compressed_and_decoded_transparently() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();
    store
        .handle(
            &owner,
            &request(
                "sessions.append_event",
                json!({"session_id":"compressed-session","event_type":"user_message","payload":{"run_id":"compressed-run","status":"completed","text":"x".repeat(12000)}}),
                "compressed-event",
            ),
            false,
        )
        .unwrap();
    let connection = rusqlite::Connection::open(&database).unwrap();
    let encoding: String = connection
        .query_row("SELECT payload_encoding FROM session_events", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(encoding, "gzip-json");
    let context = store
        .handle(
            &owner,
            &request(
                "sessions.get_history",
                json!({"session_id":"compressed-session"}),
                "compressed-history",
            ),
            false,
        )
        .unwrap();
    assert_eq!(
        context["events"][0]["payload"]["text"]
            .as_str()
            .unwrap()
            .len(),
        12000
    );
}

#[test]
fn server_summary_checkpoint_privacy_and_audit_flow() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();

    let settings = store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({"retention_days":7,"memory_enabled":true,"audit_enabled":true}),
                "settings-seven-days",
            ),
            false,
        )
        .unwrap();
    assert_eq!(settings["retention_days"], 7);

    let preference = store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({
                    "kind":"preference","text":"终端配色偏好：深色",
                    "tags":["personal-agent","user"],"sync_policy":"local_only"
                }),
                "privacy-preference",
            ),
            false,
        )
        .unwrap();
    let preference_id = preference["memory_id"].as_str().unwrap().to_owned();

    for turn in 1..=10 {
        for (role, event_type) in [("user", "user_message"), ("assistant", "assistant_message")] {
            store
                .handle(
                    &owner,
                    &request(
                        "sessions.append_event",
                        json!({
                            "session_id":"managed-session","event_type":event_type,
                            "payload":{"role":role,"run_id":format!("managed-run-{turn}"),"text":format!("{role}-{turn}")}
                        }),
                        &format!("managed-session-{turn}-{role}"),
                    ),
                    false,
                )
                .unwrap();
        }
    }
    let summarized = store
        .handle(
            &owner,
            &request(
                "sessions.summarize",
                json!({"session_id":"managed-session","interval_turns":5,"keep_raw_turns":5}),
                "summarize-managed-session",
            ),
            false,
        )
        .unwrap();
    assert_eq!(summarized["state"], "up_to_date");
    assert_eq!(summarized["source_event_sequence"], 20);
    assert_eq!(summarized["source_events_preserved"], true);
    let auto_summary = store
        .handle(
            &owner,
            &request(
                "sessions.get_context",
                json!({"session_id":"managed-session","include_events":false}),
                "managed-session-auto-summary",
            ),
            false,
        )
        .unwrap();
    assert_eq!(auto_summary["summary"]["revision"], 2);
    assert_eq!(auto_summary["summary"]["producer"], "memoryd.rules");
    assert!(auto_summary["summary"]["summary"]["current_status"]
        .as_str()
        .unwrap()
        .contains("user-1"));
    assert!(auto_summary["summary"]["summary"]["current_status"]
        .as_str()
        .unwrap()
        .contains("assistant-10"));
    let context = store
        .handle(
            &owner,
            &request(
                "sessions.get_context",
                json!({"session_id":"managed-session","include_events":true,"event_limit":100}),
                "managed-session-context",
            ),
            false,
        )
        .unwrap();
    assert_eq!(context["events"].as_array().unwrap().len(), 20);

    for checkpoint in 0..12 {
        store
            .handle(
                &owner,
                &request(
                    "runtime.prepare_checkpoint",
                    json!({"session_id":"managed-session"}),
                    &format!("checkpoint-{checkpoint}"),
                ),
                false,
            )
            .unwrap();
    }
    let checkpoints = store
        .handle(
            &owner,
            &request(
                "runtime.list_checkpoints",
                json!({"limit":10}),
                "list-checkpoints",
            ),
            false,
        )
        .unwrap();
    assert_eq!(checkpoints.as_array().unwrap().len(), 10);
    let checkpoint_ref = checkpoints[0]["checkpoint_ref"].as_str().unwrap();
    let restored = store
        .handle(
            &owner,
            &request(
                "runtime.restore_checkpoint",
                json!({"checkpoint_ref":checkpoint_ref}),
                "restore-listed-checkpoint",
            ),
            false,
        )
        .unwrap();
    assert_eq!(restored["session_id"], "managed-session");

    store
        .handle(
            &owner,
            &request(
                "audit.record",
                json!({"action":"runtime.tool.completed","details":{"tool":"terminal"}}),
                "runtime-audit-record",
            ),
            false,
        )
        .unwrap();
    let audit = store
        .handle(
            &owner,
            &request("audit.export", json!({"limit":500}), "audit-export"),
            false,
        )
        .unwrap();
    assert_eq!(audit["format"], "bianbu-agent-memory.audit.v1");
    assert!(audit["records"]
        .as_array()
        .unwrap()
        .iter()
        .any(|record| record["action"] == "runtime.tool.completed"));
    assert_eq!(
        store
            .handle(
                &owner,
                &request("audit.status", json!({}), "audit-status"),
                false
            )
            .unwrap()["state"],
        "ok"
    );

    let exported = store
        .handle(
            &owner,
            &request(
                "privacy.export_data",
                json!({"limit_per_collection":100,"include_audit":true}),
                "privacy-export",
            ),
            false,
        )
        .unwrap();
    assert_eq!(exported["format"], "bianbu-agent-memory.export.v1");
    assert_eq!(exported["session_events"].as_array().unwrap().len(), 32);
    assert!(exported["long_term_memories"]
        .as_array()
        .unwrap()
        .iter()
        .any(|memory| memory["memory_id"] == preference_id));

    // The default/standard policy keeps export available to an authenticated
    // Memory client.  Strict policy turns export into a confirmed privacy
    // operation, just like the destructive clear operations.
    store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({"sensitive_action_confirmation":"strict"}),
                "strict-export-policy",
            ),
            false,
        )
        .unwrap();
    let strict_export = store.handle(
        &owner,
        &request(
            "privacy.export_data",
            json!({"limit_per_collection":100,"include_audit":true}),
            "strict-export-denied",
        ),
        false,
    );
    assert_eq!(strict_export.unwrap_err().code, "PermissionDenied");
    store
        .handle(
            &owner,
            &request(
                "privacy.export_data",
                json!({"limit_per_collection":100,"include_audit":true,"confirm_token":"confirmed"}),
                "strict-export-confirmed",
            ),
            true,
        )
        .unwrap();
    store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({"sensitive_action_confirmation":"standard"}),
                "restore-standard-policy",
            ),
            false,
        )
        .unwrap();

    assert!(store
        .handle(
            &owner,
            &request(
                "privacy.clear_history",
                json!({"confirm_token":"confirmed"}),
                "clear-history-denied",
            ),
            false,
        )
        .is_err());
    let cleared = store
        .handle(
            &owner,
            &request(
                "privacy.clear_history",
                json!({"confirm_token":"confirmed"}),
                "clear-history-confirmed",
            ),
            true,
        )
        .unwrap();
    assert_eq!(cleared["long_term_preferences_retained"], true);
    let retained = store
        .handle(
            &owner,
            &request(
                "long_term.get_many",
                json!({"memory_ids":[preference_id]}),
                "retained-preference",
            ),
            false,
        )
        .unwrap();
    assert_eq!(retained.as_array().unwrap().len(), 1);

    store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({"memory_enabled":false}),
                "disable-memory",
            ),
            false,
        )
        .unwrap();
    let disabled = store.handle(
        &owner,
        &request(
            "long_term.search_headers",
            json!({"query":"终端","top_k":5}),
            "disabled-recall",
        ),
        false,
    );
    assert_eq!(disabled.unwrap_err().code, "MemoryDisabled");
    let permanent_settings = store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({"memory_enabled":true,"retention_days":0}),
                "enable-permanent-memory",
            ),
            false,
        )
        .unwrap();
    assert_eq!(permanent_settings["retention_days"], 0);
    assert_eq!(
        store
            .handle(
                &owner,
                &request("privacy.settings.get", json!({}), "read-permanent-memory"),
                false,
            )
            .unwrap()["retention_days"],
        0
    );

    let cleared_all = store
        .handle(
            &owner,
            &request(
                "privacy.clear_all",
                json!({"confirm_token":"confirmed","reason_code":"acceptance_test"}),
                "clear-all-confirmed",
            ),
            true,
        )
        .unwrap();
    assert_eq!(cleared_all["deleted"]["long_term_memories"], 1);
    let stats = store
        .handle(
            &owner,
            &request("system.stats", json!({}), "stats-after-clear"),
            false,
        )
        .unwrap();
    assert_eq!(stats["session_events"], 0);
    assert_eq!(stats["session_summaries"], 0);
    assert_eq!(stats["long_term_memories"], 0);
}

#[test]
fn operation_events_are_included_in_l2_summary_with_short_retention() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    // MEM-L1-01: operation events are summary content, so a summary is only due
    // once a full summary interval of completed turns exists.
    for turn in 1..=5 {
        for (role, event_type) in [("user", "user_message"), ("assistant", "assistant_message")] {
            store
                .handle(
                    &owner,
                    &request(
                        "sessions.append_event",
                        json!({
                            "session_id":"operation-session","event_type":event_type,
                            "payload":{"role":role,"run_id":format!("operation-run-{turn}"),"text":format!("{role}-{turn}")}
                        }),
                        &format!("operation-turn-{turn}-{role}"),
                    ),
                    false,
                )
                .unwrap();
        }
    }
    store.handle(&owner, &request("sessions.append_event", json!({
        "session_id":"operation-session", "event_type":"tool_result",
        "payload":{"run_id":"op-run","tool":"terminal","status":"completed","command":"echo hello","files":["/tmp/a.txt"]}
    }), "operation-event"), false).unwrap();
    let summary = store
        .handle(
            &owner,
            &request(
                "sessions.summarize",
                json!({
                    "session_id":"operation-session", "interval_turns":5, "keep_raw_turns":5
                }),
                "operation-summary",
            ),
            false,
        )
        .unwrap();
    assert_eq!(summary["state"], "summarized");
    let context = store
        .handle(
            &owner,
            &request(
                "sessions.get_context",
                json!({
                    "session_id":"operation-session", "include_events":false
                }),
                "operation-context",
            ),
            false,
        )
        .unwrap();
    assert!(context["summary"]["summary"]["commands_executed"]
        .as_array()
        .unwrap()
        .iter()
        .any(|v| v == "echo hello"));
    assert!(context["summary"]["summary"]["files_resources_touched"]
        .as_array()
        .unwrap()
        .iter()
        .any(|v| v == "/tmp/a.txt"));
}

#[test]
fn operation_events_alone_neither_trigger_nor_gate_l2_summary() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    for (index, event_type) in ["tool_call", "tool_result", "task_started"]
        .iter()
        .enumerate()
    {
        store
            .handle(
                &owner,
                &request(
                    "sessions.append_event",
                    json!({
                        "session_id":"operation-only-session","event_type":event_type,
                        "payload":{"run_id":"op-only","tool":"terminal","status":"completed","command":"echo hello"}
                    }),
                    &format!("operation-only-{index}"),
                ),
                false,
            )
            .unwrap();
    }
    let summarized = store
        .handle(
            &owner,
            &request(
                "sessions.summarize",
                json!({"session_id":"operation-only-session","interval_turns":5,"keep_raw_turns":5}),
                "operation-only-summary",
            ),
            false,
        )
        .unwrap();
    assert_eq!(summarized["state"], "not_due");
    assert_eq!(summarized["completed_turns"], 0);
    assert_eq!(summarized["revision"], 0);
    let stats = store
        .handle(
            &owner,
            &request("system.stats", json!({}), "operation-only-stats"),
            false,
        )
        .unwrap();
    assert_eq!(stats["session_summaries"], 0);
}

#[test]
fn oversized_and_control_character_memory_text_is_rejected() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let oversized_error = store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({"kind":"fact","text":"字".repeat(2201),"sync_policy":"local_only"}),
                "oversized-memory",
            ),
            false,
        )
        .unwrap_err();
    assert_eq!(oversized_error.code, "InvalidArgument");
    let control_error = store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({"kind":"fact","text":"preference\u{0}compact","sync_policy":"local_only"}),
                "control-character-memory",
            ),
            false,
        )
        .unwrap_err();
    assert_eq!(control_error.code, "InvalidArgument");
    let allowed = store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({"kind":"fact","text":"字".repeat(2200),"sync_policy":"local_only"}),
                "budget-edge-memory",
            ),
            false,
        )
        .unwrap();
    assert_eq!(allowed["revision"], 1);
}

#[test]
fn retention_windows_and_summary_archival_are_enforced() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();

    for days in [7_i64, 30, 90] {
        store
            .handle(
                &owner,
                &request(
                    "privacy.settings.update",
                    json!({"retention_days":days}),
                    &format!("retention-{days}"),
                ),
                false,
            )
            .unwrap();
        let before = memory_core::now_ms();
        let inserted = store
            .handle(
                &owner,
                &request(
                    "long_term.put",
                    json!({
                        "kind":"fact","text":format!("retention marker {days}"),
                        "sync_policy":"local_only"
                    }),
                    &format!("retention-memory-{days}"),
                ),
                false,
            )
            .unwrap();
        let record = store
            .handle(
                &owner,
                &request(
                    "long_term.get_many",
                    json!({"memory_ids":[inserted["memory_id"]]}),
                    &format!("retention-read-{days}"),
                ),
                false,
            )
            .unwrap();
        let expires_at = record[0]["expires_at"].as_i64().unwrap();
        let expected = days * 24 * 60 * 60 * 1000;
        assert!((before + expected..=memory_core::now_ms() + expected).contains(&expires_at));
    }

    store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({"retention_days":0}),
                "retention-permanent",
            ),
            false,
        )
        .unwrap();
    let permanent = store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({
                    "kind":"fact","text":"permanent retention marker",
                    "sync_policy":"local_only"
                }),
                "retention-memory-permanent",
            ),
            false,
        )
        .unwrap();
    let permanent_record = store
        .handle(
            &owner,
            &request(
                "long_term.get_many",
                json!({"memory_ids":[permanent["memory_id"]]}),
                "retention-read-permanent",
            ),
            false,
        )
        .unwrap();
    assert_eq!(permanent_record[0]["expires_at"], Value::Null);

    for turn in 1..=5 {
        for (role, event_type) in [("user", "user_message"), ("assistant", "assistant_message")] {
            store
                .handle(
                    &owner,
                    &request(
                        "sessions.append_event",
                        json!({
                            "session_id":"lifecycle-session","event_type":event_type,
                            "payload":{"role":role,"run_id":format!("lifecycle-run-{turn}"),"text":if turn == 1 && role == "user" {
                                "请记住 lifecycle stable fact".to_owned()
                            } else {
                                format!("lifecycle detail {turn} {role}")
                            }}
                        }),
                        &format!("lifecycle-{turn}-{role}"),
                    ),
                    false,
                )
                .unwrap();
        }
    }
    store
        .handle(
            &owner,
            &request(
                "sessions.summarize",
                json!({"session_id":"lifecycle-session"}),
                "lifecycle-summary",
            ),
            false,
        )
        .unwrap();

    let connection = Connection::open(&database).unwrap();
    connection
        .execute(
            "UPDATE session_summaries SET expires_at=0 WHERE session_id='lifecycle-session'",
            [],
        )
        .unwrap();
    connection
        .execute(
            "UPDATE session_events SET expires_at=0
             WHERE session_id='lifecycle-session' AND event_sequence=1",
            [],
        )
        .unwrap();
    drop(connection);

    let maintenance = store
        .handle(
            &owner,
            &request(
                "maintenance.run",
                json!({"remove_expired":true}),
                "lifecycle-maintenance",
            ),
            false,
        )
        .unwrap();
    assert_eq!(maintenance["archived_session_summaries"], 1);
    assert_eq!(maintenance["expired_session_events"], 1);

    let context = store
        .handle(
            &owner,
            &request(
                "sessions.get_context",
                json!({
                    "session_id":"lifecycle-session","include_events":true,
                    "include_summary":true,"event_limit":100
                }),
                "lifecycle-context",
            ),
            false,
        )
        .unwrap();
    assert_eq!(context["summary"], Value::Null);
    assert_eq!(context["events"].as_array().unwrap().len(), 9);

    let archived = store
        .handle(
            &owner,
            &request(
                "long_term.search_headers",
                json!({"query":"lifecycle stable fact","top_k":10}),
                "lifecycle-archive-search",
            ),
            false,
        )
        .unwrap();
    assert!(archived
        .as_array()
        .unwrap()
        .iter()
        .any(|item| item["kind"] == "episodic"));
}

#[test]
fn lifecycle_archival_includes_model_produced_summary_fields() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();

    for turn in 1..=5 {
        for (role, event_type) in [("user", "user_message"), ("assistant", "assistant_message")] {
            store
                .handle(
                    &owner,
                    &request(
                        "sessions.append_event",
                        json!({
                            "session_id":"model-archive-session","event_type":event_type,
                            "payload":{"role":role,"run_id":format!("model-archive-run-{turn}"),
                                "text":format!("model archive detail {turn} {role}")}
                        }),
                        &format!("model-archive-{turn}-{role}"),
                    ),
                    false,
                )
                .unwrap();
        }
    }
    // The fifth completed turn already triggers the inline rules summary.
    let snapshot = store
        .handle(
            &owner,
            &request(
                "sessions.get_context",
                json!({
                    "session_id":"model-archive-session","include_events":false,
                    "include_summary":true,"event_limit":1
                }),
                "model-archive-context",
            ),
            false,
        )
        .unwrap();
    let current = &snapshot["summary"];
    assert_eq!(current["producer"], "memoryd.rules");
    // The transcript carries no keyword markers, so the rules summary leaves
    // the archived sections empty and only the model patch below can fill them.
    assert!(current["summary"]["decisions"]
        .as_array()
        .unwrap()
        .is_empty());
    assert!(current["summary"]["salient_facts"]
        .as_array()
        .unwrap()
        .is_empty());

    store
        .handle(
            &owner,
            &request(
                "sessions.update_summary",
                json!({
                    "session_id":"model-archive-session",
                    "patch":{
                        "user_preferences":["mfprefalpha"],
                        "key_decisions":["mfdecalpha"],
                        "next_steps":["mfnextalpha"]
                    },
                    "source_event_sequence":current["source_event_sequence"],
                    "expected_revision":current["revision"],
                    "sync_policy":"summary_only",
                    "producer":"memoryd.model",
                    "model_ref":"test-model"
                }),
                "model-archive-update",
            ),
            false,
        )
        .unwrap();

    let connection = Connection::open(&database).unwrap();
    connection
        .execute(
            "UPDATE session_summaries SET expires_at=0 WHERE session_id='model-archive-session'",
            [],
        )
        .unwrap();
    drop(connection);

    let maintenance = store
        .handle(
            &owner,
            &request(
                "maintenance.run",
                json!({"remove_expired":true}),
                "model-archive-maintenance",
            ),
            false,
        )
        .unwrap();
    assert_eq!(maintenance["archived_session_summaries"], 1);

    let connection = Connection::open(&database).unwrap();
    let content: String = connection
        .query_row(
            "SELECT content FROM long_term_memories WHERE source='memoryd.lifecycle'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    drop(connection);
    for marker in ["mfprefalpha", "mfdecalpha", "mfnextalpha"] {
        assert!(
            content.contains(marker),
            "archived text should carry the model field {marker}: {content}"
        );
    }

    let hits = store
        .handle(
            &owner,
            &request(
                "long_term.search_headers",
                json!({"query":"mfprefalpha","top_k":10}),
                "model-archive-search",
            ),
            false,
        )
        .unwrap();
    assert!(!hits.as_array().unwrap().is_empty());
}

#[test]
fn lifecycle_archival_merges_rules_and_model_summary_fields() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();

    for turn in 1..=5 {
        for (role, event_type) in [("user", "user_message"), ("assistant", "assistant_message")] {
            let text = if turn == 1 && role == "user" {
                "请记住 mfdupalpha".to_owned()
            } else {
                format!("merge archive detail {turn} {role}")
            };
            store
                .handle(
                    &owner,
                    &request(
                        "sessions.append_event",
                        json!({
                            "session_id":"merge-archive-session","event_type":event_type,
                            "payload":{"role":role,"run_id":format!("merge-archive-run-{turn}"),"text":text}
                        }),
                        &format!("merge-archive-{turn}-{role}"),
                    ),
                    false,
                )
                .unwrap();
        }
    }
    let snapshot = store
        .handle(
            &owner,
            &request(
                "sessions.get_context",
                json!({
                    "session_id":"merge-archive-session","include_events":false,
                    "include_summary":true,"event_limit":1
                }),
                "merge-archive-context",
            ),
            false,
        )
        .unwrap();
    let current = &snapshot["summary"];
    assert_eq!(
        current["summary"]["salient_facts"]
            .as_array()
            .unwrap()
            .len(),
        1
    );

    // The model repeats the rules fact verbatim and adds one of its own: the
    // archived text must carry both without duplicating the shared fact.
    store
        .handle(
            &owner,
            &request(
                "sessions.update_summary",
                json!({
                    "session_id":"merge-archive-session",
                    "patch":{
                        "user_preferences":["请记住 mfdupalpha","mfmodelprefalpha"]
                    },
                    "source_event_sequence":current["source_event_sequence"],
                    "expected_revision":current["revision"],
                    "sync_policy":"summary_only",
                    "producer":"memoryd.model",
                    "model_ref":"test-model"
                }),
                "merge-archive-update",
            ),
            false,
        )
        .unwrap();

    let connection = Connection::open(&database).unwrap();
    connection
        .execute(
            "UPDATE session_summaries SET expires_at=0 WHERE session_id='merge-archive-session'",
            [],
        )
        .unwrap();
    drop(connection);

    let maintenance = store
        .handle(
            &owner,
            &request(
                "maintenance.run",
                json!({"remove_expired":true}),
                "merge-archive-maintenance",
            ),
            false,
        )
        .unwrap();
    assert_eq!(maintenance["archived_session_summaries"], 1);

    let connection = Connection::open(&database).unwrap();
    let content: String = connection
        .query_row(
            "SELECT content FROM long_term_memories WHERE source='memoryd.lifecycle'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    drop(connection);
    assert!(content.contains("请记住 mfdupalpha"), "{content}");
    assert!(content.contains("mfmodelprefalpha"), "{content}");
    assert_eq!(
        content.matches("请记住 mfdupalpha").count(),
        1,
        "rules and model fields must be de-duplicated: {content}"
    );
}
#[test]
fn retention_tightening_caps_stored_rows_and_maintenance_purges_them() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();

    // Cloud backup is retired: the settings view must not advertise a toggle
    // or a destination (product change 2026-10-16).
    let defaults = store
        .handle(
            &owner,
            &request("privacy.settings.get", json!({}), "retro-defaults"),
            false,
        )
        .unwrap();
    assert!(defaults.get("backup_supported").is_none());
    assert!(defaults.get("backup_enabled").is_none());

    // Permanent baseline: a fact, an instruction, and a session event, plus
    // a memory carrying an explicit 7-day expiry that must survive the cap.
    let permanent = store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({
                    "kind":"fact","text":"permanent marker before tightening",
                    "sync_policy":"local_only"
                }),
                "retro-permanent-memory",
            ),
            false,
        )
        .unwrap();
    let before_tighten = memory_core::now_ms();
    let explicit_expiry = before_tighten + 7 * 24 * 60 * 60 * 1000;
    let explicit = store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({
                    "kind":"fact","text":"explicit expiry marker","sync_policy":"local_only",
                    "expires_at":explicit_expiry
                }),
                "retro-explicit-memory",
            ),
            false,
        )
        .unwrap();
    store
        .handle(
            &owner,
            &request(
                "instructions.put",
                json!({"text":"retro permanent instruction","scope":"account"}),
                "retro-permanent-instruction",
            ),
            false,
        )
        .unwrap();
    store
        .handle(
            &owner,
            &request(
                "sessions.append_event",
                json!({
                    "session_id":"retro-session","event_type":"user_message",
                    "payload":{"role":"user","run_id":"retro-run-1","text":"retro event"}
                }),
                "retro-permanent-event",
            ),
            false,
        )
        .unwrap();

    // Age the permanent rows 40 days back so the 30-day window tightens
    // straight past them, then flip retention from permanent to 30 days.
    let permanent_id = permanent["memory_id"].as_str().unwrap();
    let explicit_id = explicit["memory_id"].as_str().unwrap();
    let aged_to = before_tighten - 40 * 24 * 60 * 60 * 1000;
    let connection = Connection::open(&database).unwrap();
    connection
        .execute(
            "UPDATE long_term_memories SET created_at=?1 WHERE memory_id=?2",
            params![aged_to, permanent_id],
        )
        .unwrap();
    connection
        .execute(
            "UPDATE instructions SET created_at=?1 WHERE text='retro permanent instruction'",
            params![aged_to],
        )
        .unwrap();
    connection
        .execute(
            "UPDATE session_events SET created_at=?1
             WHERE session_id='retro-session' AND event_sequence=1",
            params![aged_to],
        )
        .unwrap();
    drop(connection);

    let tightened = store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({"retention_days":30}),
                "retro-tighten-30",
            ),
            false,
        )
        .unwrap();
    assert_eq!(tightened["retention_restamped"], 3);

    // Restamped rows carry created_at + 30 days; the explicit 7-day stamp is
    // left exactly as it was passed because the cap never extends an expiry.
    let connection = Connection::open(&database).unwrap();
    let window: i64 = connection
        .query_row(
            "SELECT expires_at - created_at FROM long_term_memories WHERE memory_id=?1",
            params![permanent_id],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(window, 30 * 24 * 60 * 60 * 1000);
    let explicit_stamp: i64 = connection
        .query_row(
            "SELECT expires_at FROM long_term_memories WHERE memory_id=?1",
            params![explicit_id],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(explicit_stamp, explicit_expiry);
    drop(connection);

    // Repeating the same window is a no-op: every stored row is already
    // within it (regression guard: the product-config patch runs before the
    // write closure, so first-time sweeps must key off the request value).
    let repeated = store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({"retention_days":30}),
                "retro-repeat-30",
            ),
            false,
        )
        .unwrap();
    assert_eq!(repeated["retention_restamped"], 0);

    // The aged rows are already expired: one maintenance pass purges all
    // three (the aged fact, instruction, and event) while the explicit
    // marker stays alive.
    let maintenance = store
        .handle(
            &owner,
            &request(
                "maintenance.run",
                json!({"remove_expired":true}),
                "retro-maintenance",
            ),
            false,
        )
        .unwrap();
    assert_eq!(maintenance["expired_long_term_memories"], 1);
    assert_eq!(maintenance["expired_instructions"], 1);
    assert_eq!(maintenance["expired_session_events"], 1);
    let survivors = store
        .handle(
            &owner,
            &request(
                "long_term.get_many",
                json!({"memory_ids":[permanent_id, explicit_id]}),
                "retro-read-after-maintenance",
            ),
            false,
        )
        .unwrap();
    let survivor_ids: Vec<&str> = survivors
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|item| item["memory_id"].as_str())
        .collect();
    assert_eq!(survivor_ids, [explicit_id]);

    // Widening back to permanent never touches stored stamps.
    let widened = store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({"retention_days":0}),
                "retro-widen-permanent",
            ),
            false,
        )
        .unwrap();
    assert_eq!(widened["retention_restamped"], 0);
    let connection = Connection::open(&database).unwrap();
    let untouched: i64 = connection
        .query_row(
            "SELECT expires_at FROM long_term_memories WHERE memory_id=?1",
            params![explicit_id],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(untouched, explicit_expiry);
    drop(connection);
}

#[test]
fn stable_slots_can_be_audited_and_reconciled_with_tombstones() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();

    let invalid = store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({
                    "kind":"decision","text":"ambiguous slot assignment",
                    "tags":[
                        "slot:development.package_manager",
                        "slot:development.test_framework"
                    ],
                    "sync_policy":"local_only"
                }),
                "slot-invalid",
            ),
            false,
        )
        .unwrap_err();
    assert_eq!(invalid.code, "InvalidArgument");

    let older = store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({
                    "kind":"decision","text":"Python 依赖管理使用 pip",
                    "tags":["slot:development.package_manager"],
                    "sync_policy":"local_only"
                }),
                "slot-old",
            ),
            false,
        )
        .unwrap();
    let newer = store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({
                    "kind":"decision","text":"Python 依赖管理使用 uv",
                    "tags":["slot:development.package_manager"],
                    "sync_policy":"local_only"
                }),
                "slot-new",
            ),
            false,
        )
        .unwrap();

    let preview = store
        .handle(
            &owner,
            &request(
                "long_term.reconcile_slots",
                json!({"slots":["development.package_manager"],"dry_run":true}),
                "slot-preview",
            ),
            false,
        )
        .unwrap();
    assert_eq!(preview["duplicate_groups"], 1);
    assert_eq!(preview["superseded_memories"], 0);
    assert_eq!(preview["groups"][0]["winner_memory_id"], newer["memory_id"]);

    let applied = store
        .handle(
            &owner,
            &request(
                "long_term.reconcile_slots",
                json!({"slots":["development.package_manager"],"dry_run":false}),
                "slot-apply",
            ),
            false,
        )
        .unwrap();
    assert_eq!(applied["superseded_memories"], 1);

    let records = store
        .handle(
            &owner,
            &request(
                "long_term.get_many",
                json!({"memory_ids":[older["memory_id"],newer["memory_id"]]}),
                "slot-read",
            ),
            false,
        )
        .unwrap();
    assert_eq!(records.as_array().unwrap().len(), 1);
    assert_eq!(records[0]["memory_id"], newer["memory_id"]);
    assert_eq!(
        store
            .handle(
                &owner,
                &request("system.stats", json!({}), "slot-stats"),
                false,
            )
            .unwrap()["tombstones"],
        1
    );
}

#[test]
fn plaintext_database_is_migrated_to_sqlcipher_without_data_loss() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let owner = identity();
    let plain = Store::open(&database).unwrap();
    let inserted = plain
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({
                    "kind":"preference","text":"加密迁移后仍可读取",
                    "tags":["encryption-test"],"sync_policy":"local_only"
                }),
                "encryption-test-memory",
            ),
            false,
        )
        .unwrap();
    drop(plain);

    let key = [0x5a_u8; 32];
    let backup = migrate_plaintext_database(&database, &key).unwrap();
    assert!(backup.is_file());
    assert!(!directory
        .path()
        .join(".memory.db.encrypted-staging")
        .exists());
    assert!(!directory.path().join(".memory.db.config.json").exists());
    assert_eq!(
        std::fs::metadata(&database).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(
        std::fs::metadata(&backup).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert!(Store::open(&database).is_err());
    assert!(Store::open_encrypted(&database, &[0x33_u8; 32]).is_err());

    let encrypted = Store::open_encrypted(&database, &key).unwrap();
    let records = encrypted
        .handle(
            &owner,
            &request(
                "long_term.get_many",
                json!({"memory_ids":[inserted["memory_id"]]}),
                "encryption-test-read",
            ),
            false,
        )
        .unwrap();
    assert_eq!(records[0]["text"], "加密迁移后仍可读取");
}

/// A device that saved its settings on 0.1.16 keeps the retired embedding
/// endpoint on disk. The daemon must converge it, `system.health` must publish
/// what it will really use, and the file an operator reads must stop
/// disagreeing with the running process.
#[test]
fn health_reports_the_effective_embedding_endpoint_and_startup_persists_it() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let config_path = database.with_extension("config.json");
    let store = Store::open(&database).unwrap();
    let mut persisted: Value =
        serde_json::from_slice(&std::fs::read(&config_path).unwrap()).unwrap();
    persisted["embedding"]["base_url"] = json!("http://127.0.0.1:18801/v1");
    persisted["embedding"]["model"] = json!("qwen3-embedding-0.6b");
    std::fs::write(&config_path, serde_json::to_vec(&persisted).unwrap()).unwrap();
    drop(store);

    let store = Store::open(&database).unwrap();
    let health = store
        .handle(
            &identity(),
            &request("system.health", json!({}), "embedding-health"),
            false,
        )
        .unwrap();
    assert_eq!(
        health["embedding"]["base_url"],
        "http://127.0.0.1:18790/v1/embed"
    );
    assert_eq!(health["embedding"]["model"], "qwen3-embedding-0.6b-q8_0");
    assert!(health["embedding"]["enabled"].is_boolean());
    assert_eq!(health["embedding"]["config_file_matches"], true);

    // The file now agrees with the daemon, so "which endpoint is this device
    // using" has one answer instead of two.
    let on_disk: Value = serde_json::from_slice(&std::fs::read(&config_path).unwrap()).unwrap();
    assert_eq!(
        on_disk["embedding"]["base_url"],
        "http://127.0.0.1:18790/v1/embed"
    );
    assert_eq!(on_disk["embedding"]["model"], "qwen3-embedding-0.6b-q8_0");
}

/// Operators audit a device with `bianbu-agent-memoryd --report-namespaces`
/// because every identity-scoped protocol method hides namespaces nobody
/// registers any more. The report has to name them, count their rows, and list
/// the `slot:` tags they hold -- without any memory text.
#[test]
fn namespace_report_lists_spaces_with_counts_and_declared_slots() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();
    store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({
                    "kind":"profile",
                    "text":"用户希望被称呼为小圆",
                    "tags":["slot:profile.user_name"],
                    "sync_policy":"local_only"
                }),
                "namespace-report-put",
            ),
            false,
        )
        .unwrap();

    let report = report_namespaces(&database, None).unwrap();
    assert_eq!(report["storage_encryption"], "plaintext_explicit");
    assert_eq!(report["namespace_count"], 1);
    assert_eq!(report["long_term_memories"], 1);
    let namespace = &report["namespaces"][0];
    assert_eq!(namespace["account_id"], "local:test");
    assert_eq!(namespace["agent_id"], "io.bianbu.test");
    assert_eq!(namespace["memory_space_id"], "test-space");
    assert_eq!(namespace["counts"]["long_term_memories"], 1);
    assert_eq!(namespace["counts"]["session_events"], 0);
    assert_eq!(namespace["declared_slots"], json!(["profile.user_name"]));
    // The report is an inventory, not an export: no memory text travels in it.
    assert!(!report.to_string().contains("小圆"));
}

#[test]
fn health_and_stats_expose_storage_guardrails() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    wait_for_settle_window();
    let health = store
        .handle(&owner, &request("system.health", json!({}), "storage-health"), false)
        .unwrap();
    assert_eq!(health["storage"]["within_limits"], true);
    assert!(health["storage"]["free_bytes"].as_u64().unwrap() > 0);
    // The integrity verdict comes from an exclusive check, and the response
    // says how it was obtained so callers can tell "checked now" from "a writer
    // was active" (docs/GATEWAY_EMBEDDING_INTEGRATION.md §8.2).
    assert_eq!(health["status"], "ok");
    assert_eq!(health["database"], "ok");
    assert_eq!(health["database_check"]["state"], "checked");
    assert_eq!(health["database_check"]["reason"], Value::Null);
    assert_eq!(health["database_check"]["verdict"], "ok");
    assert_eq!(health["database_check"]["attempts"], 1);
    assert!(health["database_check"]["checked_at_ms"].as_i64().unwrap() > 0);
    let stats = store
        .handle(&owner, &request("system.stats", json!({}), "storage-stats"), false)
        .unwrap();
    assert_eq!(stats["storage"]["database_limit_bytes"], 512 * 1024 * 1024_i64);
}

#[test]
fn health_reports_busy_instead_of_degraded_while_a_writer_holds_the_database() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();
    // A second connection stands in for a background batch job: those take an
    // Immediate transaction without the daemon's write_lock, which is how the
    // check used to end up sampling the database mid-commit and reporting a
    // phantom FTS5 defect.
    let writer = Connection::open(&database).unwrap();
    writer.execute_batch("BEGIN IMMEDIATE;").unwrap();
    wait_for_settle_window();
    let health = store
        .handle(&owner, &request("system.health", json!({}), "busy-health"), false)
        .unwrap();
    assert_eq!(health["database"], "busy");
    assert_eq!(health["status"], "unknown");
    assert_eq!(health["database_check"]["state"], "busy");
    assert_eq!(health["database_check"]["reason"], "database-busy");
    assert_eq!(health["database_check"]["verdict"], Value::Null);
    assert_eq!(health["database_check"]["attempts"], 3);
    writer.execute_batch("COMMIT;").unwrap();
    // With the writer gone the check completes and the verdict is conclusive.
    let health = store
        .handle(&owner, &request("system.health", json!({}), "busy-health-2"), false)
        .unwrap();
    assert_eq!(health["database"], "ok");
    assert_eq!(health["status"], "ok");
    assert_eq!(health["database_check"]["state"], "checked");
}

#[test]
fn health_reuses_the_integrity_verdict_within_the_check_interval() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    wait_for_settle_window();
    let first = store
        .handle(&owner, &request("system.health", json!({}), "cached-health-1"), false)
        .unwrap();
    let second = store
        .handle(&owner, &request("system.health", json!({}), "cached-health-2"), false)
        .unwrap();
    assert_eq!(second["status"], "ok");
    assert_eq!(second["database"], "ok");
    assert_eq!(second["database_check"]["verdict"], "ok");
    // AGENT_MEMORY_HEALTH_CHECK_INTERVAL_MS=0 is the documented way to ask for
    // a fresh exclusive check per call; the default reuses the verdict.
    if second["database_check"]["interval_ms"].as_i64().unwrap() > 0 {
        assert_eq!(first["database_check"]["state"], "checked");
        assert_eq!(second["database_check"]["state"], "cached");
        assert_eq!(second["database_check"]["reason"], Value::Null);
        assert_eq!(second["database_check"]["attempts"], 0);
        assert!(second["database_check"]["age_ms"].as_i64().unwrap() >= 0);
        assert_eq!(
            second["database_check"]["checked_at_ms"],
            first["database_check"]["checked_at_ms"]
        );
        assert_eq!(second["database_check"]["complaint"], Value::Null);
    }
}

#[test]
fn health_reports_an_fts5_complaint_without_calling_the_database_degraded() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();
    let stored = store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({"kind":"fact","text":"深色主题配色偏好 zzftsdefect","sync_policy":"local_only"}),
                "fts-defect-put",
            ),
            false,
        )
        .unwrap();
    wait_for_settle_window();
    // Break the FTS5 index without touching the content table: `quick_check`
    // then reports the very text the K3 false positive produces, on every
    // sample. That text is also what an intact index reports for ~100 s after a
    // write burst, so it cannot be told apart from the artifact by sampling
    // harder and is never published as a defect
    // (docs/GATEWAY_EMBEDDING_INTEGRATION.md §8.2). It is exposed as a
    // complaint instead; retrieval is unaffected either way (see below).
    Connection::open(&database)
        .unwrap()
        .execute("UPDATE long_term_fts_docsize SET sz = zeroblob(8)", [])
        .unwrap();
    let health = store
        .handle(&owner, &request("system.health", json!({}), "fts-defect-health"), false)
        .unwrap();
    assert_eq!(health["database_check"]["state"], "complaint");
    assert_eq!(health["database_check"]["reason"], "fts5-inverted-index");
    // The sample still concludes "no defect this check can act on", so the
    // published verdict stays healthy and the complaint rides alongside it.
    assert_eq!(health["database_check"]["verdict"], "ok");
    assert!(
        health["database_check"]["complaint"]
            .as_str()
            .unwrap()
            .contains("FTS5"),
        "the raw complaint has to stay visible: {}",
        health["database_check"]["complaint"]
    );
    // Nothing was published as a verdict, so `status` is not a defect.
    assert_ne!(health["status"], "degraded");
    assert_eq!(health["status"], "ok");
    assert_eq!(health["database"], "ok");
    // Repeated calls keep reporting it - not published, but not dropped either.
    let health = store
        .handle(&owner, &request("system.health", json!({}), "fts-defect-health-2"), false)
        .unwrap();
    assert_eq!(health["database_check"]["state"], "complaint");
    assert!(
        health["database_check"]["complaint"]
            .as_str()
            .unwrap()
            .contains("FTS5")
    );
    // And what the complaint is about does not stop recall: `%_docsize` feeds
    // bm25 scoring, not matching, so FTS retrieval still finds the row.
    let found = store
        .handle(
            &owner,
            &request(
                "long_term.search_headers",
                json!({"query":"深色主题配色偏好","top_k":5}),
                "fts-defect-search",
            ),
            false,
        )
        .unwrap();
    assert!(
        found
            .as_array()
            .unwrap()
            .iter()
            .any(|header| header["memory_id"] == stored["memory_id"]),
        "recall must survive a `%_docsize` complaint: {found}"
    );
}

#[test]
fn storage_guardrail_rejects_writes_before_database_growth() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();
    store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({"max_database_bytes":16 * 1024 * 1024, "min_free_bytes":0}),
                "storage-limit",
            ),
            false,
        )
        .unwrap();
    // Simulate a database that has reached the configured ceiling without
    // changing SQLite schema contents. The guard must reject before opening a
    // write transaction and therefore before growing the WAL.
    std::fs::OpenOptions::new()
        .write(true)
        .open(&database)
        .unwrap()
        .set_len(17 * 1024 * 1024)
        .unwrap();
    let error = store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({"kind":"fact","text":"capacity check","sync_policy":"local_only"}),
                "storage-limited-write",
            ),
            false,
        )
        .unwrap_err();
    assert_eq!(error.code, "ResourceExhausted");
    assert!(error.message.contains("database storage limit"));
}

#[test]
fn summary_patches_reject_empty_and_credential_like_values() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    for (key, patch) in [
        ("empty-summary-patch", json!({})),
        (
            "secret-summary-patch",
            json!({"next_steps":["api_key=do-not-store-this"]}),
        ),
    ] {
        let error = store
            .handle(
                &owner,
                &request(
                    "sessions.update_summary",
                    json!({
                        "session_id":"safe-summary",
                        "patch":patch,
                        "source_event_sequence":0,
                        "expected_revision":0
                    }),
                    key,
                ),
                false,
            )
            .unwrap_err();
        assert_eq!(error.code, "InvalidArgument");
    }
}

// ---------------------------------------------------------------------------
// Native semantic search (MEM-NFR-02)
//
// The fake embedding server maps keyword buckets to vector axes so a query
// and its paraphrase land on the same axis ("深色"/"夜间" -> axis 0,
// "依赖"/"uv" -> axis 1). That makes semantic recall deterministic in tests:
// a paraphrased query shares an axis with the target memory while FTS sees
// no shared token at all.
// ---------------------------------------------------------------------------

const SEMANTIC_DIMS: usize = 64;

fn semantic_axis(text: &str) -> usize {
    let groups: [&[&str]; 2] = [
        &["深色", "夜间", "dark", "配色偏好"],
        &["依赖", "uv", "构建", "cargo"],
    ];
    for (axis, keywords) in groups.iter().enumerate() {
        if keywords.iter().any(|keyword| text.contains(keyword)) {
            return axis;
        }
    }
    2 + text.bytes().map(|byte| byte as usize).sum::<usize>() % (SEMANTIC_DIMS - 2)
}

fn semantic_vector(text: &str) -> Vec<f64> {
    let mut vector = vec![0.05_f64; SEMANTIC_DIMS];
    vector[semantic_axis(text)] = 1.0;
    vector
}

fn spawn_fake_embedding_server() -> u16 {
    use std::io::{BufRead, BufReader, Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    thread::spawn(move || {
        for stream in listener.incoming() {
            let mut stream = match stream {
                Ok(stream) => stream,
                Err(_) => return,
            };
            let mut reader = BufReader::new(&mut stream);
            let mut request_line = String::new();
            if reader.read_line(&mut request_line).unwrap_or(0) == 0 {
                continue;
            }
            let mut content_length = 0_usize;
            loop {
                let mut header = String::new();
                if reader.read_line(&mut header).unwrap_or(0) == 0
                    || header.trim().is_empty()
                {
                    break;
                }
                if let Some(value) = header.to_ascii_lowercase().strip_prefix("content-length:") {
                    content_length = value.trim().parse().unwrap_or(0);
                }
            }
            let mut body = vec![0_u8; content_length];
            if content_length > 0 {
                let _ = reader.read_exact(&mut body);
            }
            let texts: Vec<String> = serde_json::from_slice::<Value>(&body)
                .ok()
                .and_then(|request| {
                    request
                        .get("input")
                        .and_then(Value::as_array)
                        .map(|items| {
                            items
                                .iter()
                                .filter_map(Value::as_str)
                                .map(str::to_owned)
                                .collect()
                        })
                })
                .unwrap_or_default();
            let data: Vec<Value> = texts
                .iter()
                .enumerate()
                .map(|(index, text)| json!({"index": index, "embedding": semantic_vector(text)}))
                .collect();
            let payload = serde_json::to_vec(&json!({"data": data})).unwrap();
            let _ = stream.write_all(
                format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    payload.len()
                )
                .as_bytes(),
            );
            let _ = stream.write_all(&payload);
        }
    });
    port
}

/// Switchable fake embedding server. The returned flag selects the behaviour
/// of every request it accepts: while set, the request is read and then held
/// open without a response so the client reports its read timeout; while clear
/// the response is the same synthetic axis-aligned vector set
/// `spawn_fake_embedding_server` returns. Flipping the flag is the only way to
/// change the backend mid-test without rewriting the product config, and a
/// config write invalidates the hot-query cache the assertions observe.
fn spawn_stalling_embedding_server() -> (u16, Arc<AtomicBool>) {
    use std::io::{BufRead, BufReader, Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let stalling = Arc::new(AtomicBool::new(false));
    let flag = Arc::clone(&stalling);
    thread::spawn(move || {
        for stream in listener.incoming() {
            let stream = match stream {
                Ok(stream) => stream,
                Err(_) => return,
            };
            let flag = Arc::clone(&flag);
            thread::spawn(move || {
                let mut stream = stream;
                let mut reader = BufReader::new(&mut stream);
                let mut request_line = String::new();
                if reader.read_line(&mut request_line).unwrap_or(0) == 0 {
                    return;
                }
                let mut content_length = 0_usize;
                loop {
                    let mut header = String::new();
                    if reader.read_line(&mut header).unwrap_or(0) == 0
                        || header.trim().is_empty()
                    {
                        break;
                    }
                    if let Some(value) = header.to_ascii_lowercase().strip_prefix("content-length:") {
                        content_length = value.trim().parse().unwrap_or(0);
                    }
                }
                let mut body = vec![0_u8; content_length];
                if content_length > 0 {
                    let _ = reader.read_exact(&mut body);
                }
                if flag.load(Ordering::SeqCst) {
                    // Hold past the client's read timeout, then close without a
                    // body so the caller sees a timeout instead of an answer.
                    thread::sleep(std::time::Duration::from_millis(600));
                    return;
                }
                let texts: Vec<String> = serde_json::from_slice::<Value>(&body)
                    .ok()
                    .and_then(|request| {
                        request
                            .get("input")
                            .and_then(Value::as_array)
                            .map(|items| {
                                items
                                    .iter()
                                    .filter_map(Value::as_str)
                                    .map(str::to_owned)
                                    .collect()
                            })
                    })
                    .unwrap_or_default();
                let data: Vec<Value> = texts
                    .iter()
                    .enumerate()
                    .map(|(index, text)| json!({"index": index, "embedding": semantic_vector(text)}))
                    .collect();
                let payload = serde_json::to_vec(&json!({"data": data})).unwrap();
                let _ = stream.write_all(
                    format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        payload.len()
                    )
                    .as_bytes(),
                );
                let _ = stream.write_all(&payload);
            });
        }
    });
    (port, stalling)
}

fn enable_embedding(store: &Store, owner: &MemoryIdentity, port: u16, key: &str) -> Value {
    store
        .handle(
            owner,
            &request(
                "privacy.settings.update",
                json!({
                    "embedding":{
                        "enabled":true,
                        "base_url":format!("http://127.0.0.1:{port}/v1"),
                        "model":"test-embedding",
                        "dims":SEMANTIC_DIMS,
                        "query_instruct":""
                    }
                }),
                key,
            ),
            false,
        )
        .unwrap()
}

fn embedding_count(store: &Store, owner: &MemoryIdentity) -> i64 {
    let stats = store
        .handle(owner, &request("system.stats", json!({}), "embedding-stats"), false)
        .unwrap();
    stats["memory_embeddings"].as_i64().unwrap_or(-1)
}

fn put_memory(
    store: &Store,
    owner: &MemoryIdentity,
    text: &str,
    key: &str,
) -> Value {
    store
        .handle(
            owner,
            &request(
                "long_term.put",
                json!({"kind":"preference","text":text,"sync_policy":"local_only"}),
                key,
            ),
            false,
        )
        .unwrap()
}

fn search(store: &Store, owner: &MemoryIdentity, query: &str, key: &str) -> Vec<Value> {
    store
        .handle(
            owner,
            &request(
                "long_term.search_headers",
                json!({"query":query,"top_k":10}),
                key,
            ),
            false,
        )
        .unwrap()
        .as_array()
        .cloned()
        .unwrap_or_default()
}

#[test]
fn put_embeds_and_semantic_recall_hits_paraphrase() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let port = spawn_fake_embedding_server();
    enable_embedding(&store, &owner, port, "enable-embedding-put");

    assert_eq!(embedding_count(&store, &owner), 0);
    let dark = put_memory(
        &store,
        &owner,
        "终端与编辑器使用深色主题配色",
        "semantic-put-dark",
    );
    put_memory(&store, &owner, "项目构建常用 cargo build --release", "semantic-put-build");
    assert_eq!(embedding_count(&store, &owner), 2);

    // The paraphrase shares no token with the stored text, so FTS cannot
    // match it; only the vector stage can.
    let hits = search(&store, &owner, "夜间模式怎么设置", "semantic-search-dark");
    assert!(!hits.is_empty(), "semantic recall must hit the paraphrase");
    assert_eq!(hits[0]["memory_id"], dark["memory_id"]);
}

#[test]
fn degraded_semantic_stage_is_not_memoized_by_the_search_cache() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let (port, stalling) = spawn_stalling_embedding_server();
    // A short per-request budget keeps the degradation cheap to reproduce. The
    // update carries the whole embedding section because a settings patch
    // replaces that section rather than merging into it.
    store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({
                    "embedding":{
                        "enabled":true,
                        "base_url":format!("http://127.0.0.1:{port}/v1"),
                        "model":"test-embedding",
                        "dims":SEMANTIC_DIMS,
                        "query_instruct":"",
                        "max_latency_ms":300
                    }
                }),
                "degraded-enable",
            ),
            false,
        )
        .unwrap();
    let dark = put_memory(&store, &owner, "终端与编辑器使用深色主题配色", "degraded-put");
    assert_eq!(embedding_count(&store, &owner), 1);

    // The paraphrase shares no token with the stored text, so the lexical
    // cascade cannot answer it: an empty result is the visible symptom of a
    // degraded semantic stage, not of a memory that was never written.
    let query = "夜间模式怎么设置";
    stalling.store(true, Ordering::SeqCst);
    let started = std::time::Instant::now();
    let degraded = search(&store, &owner, query, "degraded-search-1");
    let elapsed = started.elapsed();
    assert!(degraded.is_empty(), "the lexical stage has no overlap here");
    assert!(
        elapsed >= std::time::Duration::from_millis(250),
        "the query must have reached the embedding stage and paid its timeout, took {elapsed:?}"
    );

    // With the backend healthy again the same question is recomputed and finds
    // the paraphrase. Before the fix the memoized degraded answer is served
    // instead, so this is the regression the test pins down.
    stalling.store(false, Ordering::SeqCst);
    let recovered = search(&store, &owner, query, "degraded-search-2");
    assert!(
        !recovered.is_empty(),
        "a degraded answer must not be memoized for the query that produced it"
    );
    assert_eq!(recovered[0]["memory_id"], dark["memory_id"]);

    // And the opposite direction: an answer the semantic stage did compute is
    // still memoized. With the backend stalling again, only a cache hit can
    // return it.
    stalling.store(true, Ordering::SeqCst);
    let repeat = search(&store, &owner, query, "degraded-search-3");
    assert_eq!(repeat, recovered, "a computed answer must still be cached");
}

#[test]
fn embedding_backfill_drains_a_backlog_larger_than_one_batch() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();

    // Embedding off while the memories are written, so they form a backlog the
    // moment it is enabled.
    store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({"embedding":{"enabled":false}}),
                "backlog-disable",
            ),
            false,
        )
        .unwrap();
    for index in 0..5 {
        put_memory(
            &store,
            &owner,
            &format!("批量回填候选 {index}"),
            &format!("backlog-put-{index}"),
        );
    }
    assert_eq!(embedding_count(&store, &owner), 0);

    // Backdate three rows far outside the marker's 2 s recency slack. A marker
    // that advanced after a partial batch would hide exactly these rows from
    // every later pass, which is the stranding this test pins down.
    {
        let connection = Connection::open(&database).unwrap();
        connection
            .execute(
                "UPDATE long_term_memories SET updated_at=updated_at-600000
                 WHERE memory_id IN (
                    SELECT memory_id FROM long_term_memories ORDER BY rowid LIMIT 3)",
                [],
            )
            .unwrap();
    }

    let port = spawn_fake_embedding_server();
    // batch_size 2 makes the per-request item cap the binding constraint.
    store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({
                    "embedding":{
                        "enabled":true,
                        "base_url":format!("http://127.0.0.1:{port}/v1"),
                        "model":"test-embedding",
                        "dims":SEMANTIC_DIMS,
                        "query_instruct":"",
                        "batch_size":2
                    }
                }),
                "backlog-enable",
            ),
            false,
        )
        .unwrap();

    // Each pass indexes at most one batch, and whatever it leaves behind stays
    // visible to the next pass instead of falling behind the recency marker.
    let mut counts = Vec::new();
    for _ in 0..4 {
        store.run_background_jobs("k3-backlog").unwrap();
        counts.push(embedding_count(&store, &owner));
        let connection = Connection::open(&database).unwrap();
        // The job schedule throttles each kind to one pass per minute; rewind it
        // so the test can run the passes back to back.
        connection
            .execute("UPDATE background_worker_state SET next_retry_at=0", [])
            .unwrap();
    }
    assert_eq!(counts, vec![2, 4, 5, 5]);
}

#[test]
fn inline_embed_budget_bounds_the_write_ack_and_the_backfill_finishes_the_vector() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();
    let (port, stalling) = spawn_stalling_embedding_server();
    stalling.store(true, Ordering::SeqCst);

    // A 200 ms inline budget against a backend that never answers: the write
    // must return on that budget instead of waiting out index_timeout_ms.
    store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({
                    "embedding":{
                        "enabled":true,
                        "base_url":format!("http://127.0.0.1:{port}/v1"),
                        "model":"test-embedding",
                        "dims":SEMANTIC_DIMS,
                        "query_instruct":"",
                        "inline_timeout_ms":200,
                        "index_timeout_ms":30000
                    }
                }),
                "inline-budget-enable",
            ),
            false,
        )
        .unwrap();

    let started = std::time::Instant::now();
    put_memory(&store, &owner, "inline budget bounds the write ack", "inline-budget-put");
    let elapsed = started.elapsed();
    assert_eq!(
        embedding_count(&store, &owner),
        0,
        "the vector is deferred to the backfill, not lost"
    );
    // Floors the other way too: a put that never reached the backend at all
    // would satisfy an upper bound while proving nothing.
    assert!(
        elapsed >= std::time::Duration::from_millis(150),
        "put returned in {elapsed:?}; the inline embed never waited, so the budget was not exercised"
    );
    assert!(
        elapsed < std::time::Duration::from_secs(5),
        "put waited {elapsed:?}; the inline budget did not bound the write ack"
    );

    // Once the backend answers again, the next background pass indexes the same
    // row: the inline budget hands the vector off, it does not drop it.
    stalling.store(false, Ordering::SeqCst);
    for _ in 0..2 {
        store.run_background_jobs("k3-inline-budget").unwrap();
        if embedding_count(&store, &owner) == 1 {
            break;
        }
        let connection = Connection::open(&database).unwrap();
        connection
            .execute("UPDATE background_worker_state SET next_retry_at=0", [])
            .unwrap();
    }
    assert_eq!(embedding_count(&store, &owner), 1);
}

#[test]
fn embedding_outage_degrades_silently_to_fts() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    // Port 1 is never listening: every embedding call fails.
    enable_embedding(&store, &owner, 1, "enable-embedding-dead");

    let dark = put_memory(
        &store,
        &owner,
        "终端与编辑器使用深色主题配色",
        "dead-put-dark",
    );
    assert_eq!(embedding_count(&store, &owner), 0, "put stays best-effort");

    // The query shares tokens with the stored text: FTS fallback must work.
    let hits = search(&store, &owner, "深色主题", "dead-search-dark");
    assert!(!hits.is_empty());
    assert_eq!(hits[0]["memory_id"], dark["memory_id"]);
}

#[test]
fn forget_and_clear_all_drop_embedding_vectors() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let port = spawn_fake_embedding_server();
    enable_embedding(&store, &owner, port, "enable-embedding-lifecycle");

    let first = put_memory(&store, &owner, "终端使用深色主题", "lifecycle-put-1");
    put_memory(&store, &owner, "使用 uv 管理依赖", "lifecycle-put-2");
    assert_eq!(embedding_count(&store, &owner), 2);

    store
        .handle(
            &owner,
            &request(
                "long_term.forget",
                json!({"memory_id":first["memory_id"],"confirm_token":"verified","reason_code":"test"}),
                "lifecycle-forget",
            ),
            true,
        )
        .unwrap();
    assert_eq!(embedding_count(&store, &owner), 1);

    store
        .handle(
            &owner,
            &request(
                "privacy.clear_all",
                json!({"confirm_token":"verified","reason_code":"test"}),
                "lifecycle-clear-all",
            ),
            true,
        )
        .unwrap();
    assert_eq!(embedding_count(&store, &owner), 0);
}

#[test]
fn semantic_cache_serves_writes_between_queries() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let port = spawn_fake_embedding_server();
    enable_embedding(&store, &owner, port, "enable-embedding-cache");

    // First search populates the in-process snapshot cache.
    let dark = put_memory(
        &store,
        &owner,
        "终端与编辑器使用深色主题配色",
        "cache-put-dark",
    );
    let hits = search(&store, &owner, "夜间模式怎么设置", "cache-search-1");
    assert!(!hits.is_empty());
    assert_eq!(hits[0]["memory_id"], dark["memory_id"]);

    // A put after the cache exists must be visible on the very next query:
    // small namespaces reload on every watermark change, so the
    // put-then-ask pattern keeps working through the vector branch.
    let uv = put_memory(&store, &owner, "使用 uv 管理 Python 依赖", "cache-put-uv");
    let hits = search(&store, &owner, "怎么管理 Python 依赖", "cache-search-2");
    assert!(!hits.is_empty());
    assert_eq!(hits[0]["memory_id"], uv["memory_id"]);

    // A forgotten memory must not resurface from the cached snapshot.
    store
        .handle(
            &owner,
            &request(
                "long_term.forget",
                json!({"memory_id":uv["memory_id"],"confirm_token":"verified","reason_code":"test"}),
                "cache-forget-uv",
            ),
            true,
        )
        .unwrap();
    let hits = search(&store, &owner, "怎么管理 Python 依赖", "cache-search-3");
    assert!(
        hits.iter().all(|hit| hit["memory_id"] != uv["memory_id"]),
        "a forgotten memory must not surface from the semantic stage"
    );
}

#[test]
fn revise_marks_stale_and_background_job_reembeds() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let port = spawn_fake_embedding_server();
    enable_embedding(&store, &owner, port, "enable-embedding-revise");

    let memory = put_memory(&store, &owner, "终端使用深色主题", "revise-put");
    assert_eq!(embedding_count(&store, &owner), 1);

    // Revise with embedding temporarily off: the stored vector keeps the old
    // revision, so the semantic stage must not use it.
    store
        .handle(
            &owner,
            &request(
                "privacy.settings.update",
                json!({"embedding":{"enabled":false}}),
                "revise-disable-embedding",
            ),
            false,
        )
        .unwrap();
    let revised = store
        .handle(
            &owner,
            &request(
                "long_term.revise",
                json!({
                    "memory_id":memory["memory_id"],
                    "text":"项目构建常用 cargo build --release",
                    "expected_revision":1
                }),
                "revise-text",
            ),
            false,
        )
        .unwrap();
    assert_eq!(revised["revision"], 2);
    assert_eq!(embedding_count(&store, &owner), 1, "stale row is kept for backfill");
    // Re-enable BEFORE the stale search so the vector branch actually runs:
    // an empty hit then proves the stale-revision vector is excluded, not
    // that the branch was off. (Re-enabling does not inline-reembed a revise,
    // so the stale row stays stale until the background job below.)
    enable_embedding(&store, &owner, port, "revise-re-enable-embedding");
    let hits = search(&store, &owner, "夜间模式", "revise-search-stale");
    assert!(
        hits.is_empty(),
        "a stale-revision vector must not surface the memory"
    );

    // Let the background embedding job rebuild the vector.
    let jobs = store.run_background_jobs("k3-test").unwrap();
    let embedding_job = jobs["results"]
        .as_array()
        .unwrap()
        .iter()
        .find(|entry| entry["job_kind"] == "embedding")
        .cloned()
        .unwrap_or(json!(null));
    assert_eq!(embedding_job["result"]["state"], "success");
    assert_eq!(embedding_job["result"]["indexed"], 1);
    let hits = search(&store, &owner, "怎么管理 Python 依赖", "revise-search-fresh");
    assert!(!hits.is_empty(), "backfilled vector restores semantic recall");
    assert_eq!(hits[0]["memory_id"], memory["memory_id"]);
}

#[test]
fn embeddings_stay_namespaced_and_skip_secrets() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let mut other = identity();
    other.agent_id = "io.bianbu.other".to_owned();
    other.memory_space_id = "other-space".to_owned();
    let port = spawn_fake_embedding_server();
    enable_embedding(&store, &owner, port, "enable-embedding-privacy");
    enable_embedding(&store, &other, port, "enable-embedding-privacy-other");

    put_memory(&store, &owner, "终端使用深色主题", "privacy-put-dark");
    // Explicit secrets and credential-like text never reach the embedder.
    store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({
                    "kind":"fact",
                    "text":"服务器密码是 hunter2",
                    "sensitivity":"secret",
                    "sync_policy":"local_only"
                }),
                "privacy-put-secret",
            ),
            false,
        )
        .unwrap();
    assert_eq!(embedding_count(&store, &owner), 1);
    assert_eq!(embedding_count(&store, &other), 0, "vectors never cross namespaces");

    let foreign = search(&store, &other, "夜间模式", "privacy-search-other");
    assert!(foreign.is_empty(), "namespace isolation holds on the scan path");
}

#[test]
fn embedding_settings_validation_is_enforced_on_the_api_path() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    for (key, patch) in [
        (
            "embedding-https-rejected",
            json!({"embedding":{"enabled":true,"base_url":"https://127.0.0.1:18801/v1","model":"m","dims":1024}}),
        ),
        (
            "embedding-missing-endpoint",
            // base_url must be an explicit null: since the defaults now ship
            // an endpoint, a merely-absent field is backfilled from them by
            // the struct-level serde(default) and would pass validation.
            json!({"embedding":{"enabled":true,"base_url":null,"dims":1024}}),
        ),
        (
            "embedding-bad-dims",
            json!({"embedding":{"enabled":true,"base_url":"http://127.0.0.1:18801/v1","model":"m","dims":8}}),
        ),
    ] {
        let error = store
            .handle(&owner, &request("privacy.settings.update", patch, key), false)
            .unwrap_err();
        assert_eq!(error.code, "InvalidArgument", "case {key}");
    }
}

#[test]
fn search_cache_serves_repeats_and_refreshes_after_writes() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    put_memory(&store, &owner, "终端使用深色主题", "cache-put-1");

    let first = search(&store, &owner, "终端 主题", "cache-search-1");
    assert_eq!(first.len(), 1);
    // Repeats of an unchanged query (served from the MEM-NFR-01-01
    // hot-query cache after the first pass) return identical results.
    let repeat = search(&store, &owner, "终端 主题", "cache-search-2");
    assert_eq!(first, repeat);

    // A committed write must invalidate: the new row surfaces immediately
    // instead of the memoized pre-write answer.
    put_memory(&store, &owner, "浏览器主题跟随系统深色", "cache-put-2");
    let after = search(&store, &owner, "终端 主题", "cache-search-3");
    assert_eq!(after.len(), 2, "post-write search must see both rows");
}

#[test]
fn search_cache_keys_cover_top_k_and_kind_filters() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    put_memory(&store, &owner, "终端使用深色主题", "cache-kinds-put-1");
    store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({"kind":"fact","text":"主题设置存放在配置文件","sync_policy":"local_only"}),
                "cache-kinds-put-2",
            ),
            false,
        )
        .unwrap();

    let wide = search(&store, &owner, "主题", "cache-kinds-search-1");
    assert_eq!(wide.len(), 2);
    // The same query with a narrower top_k must not be served the wider
    // cached result: the cache key covers top_k.
    let narrow = store
        .handle(
            &owner,
            &request(
                "long_term.search_headers",
                json!({"query":"主题","top_k":1}),
                "cache-kinds-search-2",
            ),
            false,
        )
        .unwrap()
        .as_array()
        .cloned()
        .unwrap_or_default();
    assert_eq!(narrow.len(), 1);
    // And a kinds filter must not be served the unfiltered cached result:
    // the cache key covers the kinds list.
    let facts = store
        .handle(
            &owner,
            &request(
                "long_term.search_headers",
                json!({"query":"主题","top_k":10,"kinds":["fact"]}),
                "cache-kinds-search-3",
            ),
            false,
        )
        .unwrap()
        .as_array()
        .cloned()
        .unwrap_or_default();
    assert_eq!(facts.len(), 1);
    assert_eq!(facts[0]["kind"], "fact");
    // Repeating the unfiltered shape still returns the full set.
    assert_eq!(search(&store, &owner, "主题", "cache-kinds-search-4").len(), 2);
}

#[test]
fn search_cache_keeps_namespaces_isolated() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let mut other = identity();
    other.memory_space_id = "cache-other-space".to_owned();
    let mine = put_memory(&store, &owner, "夜间模式跟随系统", "cache-space-put-1");
    let theirs = put_memory(&store, &other, "夜间模式保持浅色", "cache-space-put-2");

    // The same query under two identities must never cross-serve cached
    // results: the cache key covers the full identity.
    for key in ["cache-space-search-1", "cache-space-search-2"] {
        let owner_hits = search(&store, &owner, "夜间模式", key);
        assert_eq!(owner_hits.len(), 1);
        assert_eq!(owner_hits[0]["memory_id"], mine["memory_id"]);
    }
    for key in ["cache-space-search-3", "cache-space-search-4"] {
        let other_hits = search(&store, &other, "夜间模式", key);
        assert_eq!(other_hits.len(), 1);
        assert_eq!(other_hits[0]["memory_id"], theirs["memory_id"]);
    }
}

#[test]
fn forget_and_clear_all_invalidate_cached_search_results() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    let memory = put_memory(&store, &owner, "终端使用深色主题", "cache-forget-put-1");
    assert_eq!(search(&store, &owner, "终端 主题", "cache-forget-search-1").len(), 1);

    store
        .handle(
            &owner,
            &request(
                "long_term.forget",
                json!({
                    "memory_id":memory["memory_id"],
                    "confirm_token":"verified",
                    "reason_code":"test"
                }),
                "cache-forget-forget",
            ),
            true,
        )
        .unwrap();
    assert!(
        search(&store, &owner, "终端 主题", "cache-forget-search-2").is_empty(),
        "forget must drop the memoized result, not serve the deleted row"
    );

    put_memory(&store, &owner, "浏览器默认深色", "cache-clear-put-2");
    assert_eq!(search(&store, &owner, "浏览器 深色", "cache-clear-search-1").len(), 1);
    store
        .handle(
            &owner,
            &request(
                "privacy.clear_all",
                json!({"confirm_token":"verified","reason_code":"test"}),
                "cache-clear-all",
            ),
            true,
        )
        .unwrap();
    assert!(
        search(&store, &owner, "浏览器 深色", "cache-clear-search-2").is_empty(),
        "clear_all must drop the memoized result"
    );
}

/// Feed one session whose facts are spread across turns, then check that the
/// rules summary records, per fact, the session event it was extracted from.
fn append_summary_provenance_turns(
    store: &Store,
    owner: &MemoryIdentity,
    session: &str,
    prefix: &str,
) {
    let assistant_text =
        "已完成 crates/memory-store/src/embedding.rs 的裁剪，执行 cargo test --workspace 通过。";
    for turn in 1..=5 {
        let user_text = format!(
            "我决定默认用 uv 管理依赖 {turn}。请记住我喜欢深色主题 {turn}。待办还要补齐 K3 证据 {turn}。"
        );
        for (role, event_type, text) in [
            ("user", "user_message", user_text),
            ("assistant", "assistant_message", assistant_text.to_owned()),
        ] {
            store
                .handle(
                    owner,
                    &request(
                        "sessions.append_event",
                        json!({
                            "session_id":session,"event_type":event_type,
                            "payload":{"role":role,"run_id":format!("{prefix}-run-{turn}"),
                                "text":text}
                        }),
                        &format!("{prefix}-{turn}-{role}"),
                    ),
                    false,
                )
                .unwrap();
        }
    }
}

#[test]
fn summary_extraction_records_the_event_behind_each_fact() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();
    let session = "source-sequence-session";

    append_summary_provenance_turns(&store, &owner, session, "source-sequence");

    let snapshot = store
        .handle(
            &owner,
            &request(
                "sessions.get_context",
                json!({
                    "session_id":session,"include_events":false,"include_summary":true,
                    "event_limit":1
                }),
                "source-sequence-context",
            ),
            false,
        )
        .unwrap();
    assert_eq!(snapshot["summary"]["producer"], "memoryd.rules");
    let summary = &snapshot["summary"]["summary"];
    let origins = &summary["fact_source_sequences"];
    for key in [
        "decisions",
        "unresolved_tasks",
        "salient_facts",
        "files_resources_touched",
        "commands_executed",
        "operations_completed",
        "errors_encountered",
    ] {
        assert_eq!(
            summary[key].as_array().unwrap().len(),
            origins[key].as_array().unwrap().len(),
            "provenance for {key} must stay index-aligned with its items"
        );
    }
    // One sentence per user turn, one event sequence apart, no 1200-byte blob.
    assert_eq!(
        summary["decisions"],
        json!([
            "我决定默认用 uv 管理依赖 1",
            "我决定默认用 uv 管理依赖 2",
            "我决定默认用 uv 管理依赖 3",
            "我决定默认用 uv 管理依赖 4",
            "我决定默认用 uv 管理依赖 5",
        ])
    );
    assert_eq!(origins["decisions"], json!([1, 3, 5, 7, 9]));
    assert_eq!(origins["salient_facts"], json!([1, 3, 5, 7, 9]));
    assert_eq!(origins["unresolved_tasks"], json!([1, 3, 5, 7, 9]));
    // The assistant repeats itself, so only the first occurrence is kept.
    assert_eq!(
        summary["files_resources_touched"],
        json!(["crates/memory-store/src/embedding.rs"])
    );
    assert_eq!(origins["files_resources_touched"], json!([2]));
    assert_eq!(
        summary["commands_executed"],
        json!(["cargo test --workspace"])
    );
    assert_eq!(origins["commands_executed"], json!([2]));
    assert!(summary["errors_encountered"].as_array().unwrap().is_empty());
    // Assistant prose must never be classified as a user decision or fact.
    assert!(!summary["decisions"]
        .as_array()
        .unwrap()
        .iter()
        .any(|item| item.as_str().unwrap().contains("已完成")));

    // Every recorded sequence must resolve to the session event it claims.
    let connection = Connection::open(&database).unwrap();
    for key in ["decisions", "salient_facts", "unresolved_tasks"] {
        for sequence in origins[key].as_array().unwrap() {
            let sequence = sequence.as_i64().unwrap();
            let blob: Vec<u8> = connection
                .query_row(
                    "SELECT payload FROM session_events WHERE session_id=?1 AND event_sequence=?2",
                    params![session, sequence],
                    |row| row.get(0),
                )
                .unwrap();
            let event: Value = serde_json::from_slice(&blob).unwrap();
            let text = event["text"].as_str().unwrap();
            assert!(
                text.contains("我决定默认用 uv 管理依赖")
                    || text.contains("请记住我喜欢深色主题")
                    || text.contains("待办还要补齐 K3 证据"),
                "sequence {sequence} must hold the fact it is recorded for"
            );
        }
    }
}

#[test]
fn archived_summary_records_the_event_sequences_it_was_built_from() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let store = Store::open(&database).unwrap();
    let owner = identity();
    let session = "archive-provenance-session";

    append_summary_provenance_turns(&store, &owner, session, "archive-provenance");

    let connection = Connection::open(&database).unwrap();
    connection
        .execute(
            "UPDATE session_summaries SET expires_at=0 WHERE session_id=?1",
            params![session],
        )
        .unwrap();
    drop(connection);

    let maintenance = store
        .handle(
            &owner,
            &request(
                "maintenance.run",
                json!({"remove_expired":true}),
                "archive-provenance-maintenance",
            ),
            false,
        )
        .unwrap();
    assert_eq!(maintenance["archived_session_summaries"], 1);

    let connection = Connection::open(&database).unwrap();
    let (memory_id, tags_json, content): (String, String, String) = connection
        .query_row(
            "SELECT memory_id,tags_json,content FROM long_term_memories
             WHERE source='memoryd.lifecycle'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    assert!(content.contains("我决定默认用 uv 管理依赖 1"));
    assert!(content.contains("请记住我喜欢深色主题 5"));
    assert!(content.contains("待办还要补齐 K3 证据 3"));
    let tags: Value = serde_json::from_str(&tags_json).unwrap();
    let pointer = tags
        .as_array()
        .unwrap()
        .iter()
        .filter_map(Value::as_str)
        .find(|tag| tag.starts_with(&format!("session:{session}:events:")))
        .expect("an archived summary must point back at its session events");
    let listed = pointer
        .rsplit(':')
        .next()
        .unwrap()
        .split(',')
        .map(|value| value.parse::<i64>().unwrap())
        .collect::<Vec<_>>();
    assert_eq!(listed, vec![1, 3, 5, 7, 9]);

    let payload: Value = serde_json::from_slice(
        &connection
            .query_row(
                "SELECT payload FROM memory_revisions WHERE memory_id=?1 AND revision=1",
                params![memory_id],
                |row| row.get::<_, Vec<u8>>(0),
            )
            .unwrap(),
    )
    .unwrap();
    let recorded = payload["source_event_sequences"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(Value::as_i64)
        .collect::<Vec<_>>();
    assert_eq!(recorded, listed);
    for sequence in &recorded {
        let count: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM session_events WHERE session_id=?1 AND event_sequence=?2",
                params![session, sequence],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(
            count, 1,
            "source sequence {sequence} must resolve to a session event"
        );
    }
}

#[test]
fn export_can_be_imported_back_after_clear_and_re_import_is_idempotent() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();

    store
        .handle(
            &owner,
            &request(
                "sessions.append_event",
                json!({
                    "session_id":"session-import",
                    "event_type":"user_message",
                    "payload":{"text":"remember that the build uses ninja"}
                }),
                "import-event",
            ),
            false,
        )
        .unwrap();
    let memory_id = store
        .handle(
            &owner,
            &request(
                "long_term.put",
                json!({
                    "kind":"preference",
                    "text":"We build with ninja on the K3 board",
                    "tags":["important"],
                    "sync_policy":"local_only"
                }),
                "import-memory",
            ),
            false,
        )
        .unwrap()["memory_id"]
        .as_str()
        .unwrap()
        .to_owned();
    store
        .handle(
            &owner,
            &request(
                "sessions.update_summary",
                json!({
                    "session_id":"session-import",
                    "patch":{"current_status":"imported"},
                    "source_event_sequence":1,
                    "expected_revision":0
                }),
                "import-summary",
            ),
            false,
        )
        .unwrap();

    let exported = store
        .handle(
            &owner,
            &request(
                "privacy.export_data",
                json!({"limit_per_collection":500,"include_audit":false}),
                "import-export",
            ),
            false,
        )
        .unwrap();
    assert_eq!(exported["session_events"].as_array().unwrap().len(), 1);
    assert_eq!(exported["session_summaries"].as_array().unwrap().len(), 1);
    assert_eq!(exported["long_term_memories"].as_array().unwrap().len(), 1);

    store
        .handle(
            &owner,
            &request(
                "privacy.clear_all",
                json!({"confirm_token":"confirmed","reason_code":"user_request"}),
                "import-clear",
            ),
            true,
        )
        .unwrap();
    let cleared = store
        .handle(
            &owner,
            &request("system.stats", json!({}), "import-stats-cleared"),
            false,
        )
        .unwrap();
    assert_eq!(cleared["long_term_memories"], 0);

    let restored = store
        .handle(
            &owner,
            &request(
                "privacy.import_data",
                json!({"payload":exported,"confirm_token":"confirmed"}),
                "import-restore",
            ),
            true,
        )
        .unwrap();
    assert_eq!(restored["imported"]["session_events"], 1);
    assert_eq!(restored["imported"]["session_summaries"], 1);
    assert_eq!(restored["imported"]["long_term_memories"], 1);
    assert_eq!(restored["skipped"]["long_term_memories"], 0);

    let recalled = store
        .handle(
            &owner,
            &request(
                "long_term.get_many",
                json!({"memory_ids":[memory_id]}),
                "import-recall",
            ),
            false,
        )
        .unwrap();
    let record = &recalled[0];
    assert_eq!(record["text"], "We build with ninja on the K3 board");
    assert_eq!(record["tags"][0], "important");

    // Re-importing the same document is a no-op: every record already exists,
    // so nothing is duplicated and every record is reported as skipped.
    let repeated = store
        .handle(
            &owner,
            &request(
                "privacy.import_data",
                json!({"payload":exported,"confirm_token":"confirmed"}),
                "import-repeat",
            ),
            true,
        )
        .unwrap();
    assert_eq!(repeated["imported"]["session_events"], 0);
    assert_eq!(repeated["imported"]["long_term_memories"], 0);
    assert_eq!(repeated["skipped"]["session_events"], 1);
    assert_eq!(repeated["skipped"]["session_summaries"], 1);
    assert_eq!(repeated["skipped"]["long_term_memories"], 1);
    let stats = store
        .handle(
            &owner,
            &request("system.stats", json!({}), "import-stats"),
            false,
        )
        .unwrap();
    assert_eq!(stats["long_term_memories"], 1);
    assert_eq!(stats["session_events"], 1);
}

#[test]
fn import_rejects_foreign_documents_and_requires_a_confirmation_token() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();

    let unconfirmed = store
        .handle(
            &owner,
            &request(
                "privacy.import_data",
                json!({"payload":{"format":"bianbu-agent-memory.export.v1"}}),
                "import-unconfirmed",
            ),
            false,
        )
        .unwrap_err();
    assert_eq!(unconfirmed.code, "PermissionDenied");

    let wrong_format = store
        .handle(
            &owner,
            &request(
                "privacy.import_data",
                json!({"payload":{"format":"something.else.v1"},"confirm_token":"confirmed"}),
                "import-wrong-format",
            ),
            true,
        )
        .unwrap_err();
    assert_eq!(wrong_format.code, "InvalidArgument");

    let foreign_scope = store
        .handle(
            &owner,
            &request(
                "privacy.import_data",
                json!({
                    "payload":{
                        "format":"bianbu-agent-memory.export.v1",
                        "scope":{
                            "account_id":"local:test",
                            "agent_id":"io.bianbu.other",
                            "memory_space_id":"test-space"
                        }
                    },
                    "confirm_token":"confirmed"
                }),
                "import-foreign-scope",
            ),
            true,
        )
        .unwrap_err();
    assert_eq!(foreign_scope.code, "InvalidArgument");
}

#[test]
fn export_pages_cover_more_than_500_events_without_truncation() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    for index in 0..501 {
        store
            .handle(
                &owner,
                &request(
                    "sessions.append_event",
                    json!({
                        "session_id":"export-session",
                        "event_type":"user_message",
                        "payload":{"text":format!("export event {index}")}
                    }),
                    &format!("export-event-{index}"),
                ),
                false,
            )
            .unwrap();
    }

    let mut page = store
        .handle(
            &owner,
            &request(
                "privacy.export_data",
                json!({"limit_per_collection":500,"include_audit":false}),
                "export-page-1",
            ),
            false,
        )
        .unwrap();
    assert_eq!(page["session_events"].as_array().unwrap().len(), 500);
    assert_eq!(page["has_more"]["session_events"], true);
    assert_eq!(page["has_more_any"], true);

    let snapshot_at = page["snapshot_at"].as_i64().unwrap();
    let cursor = page["next_cursor"].clone();
    page = store
        .handle(
            &owner,
            &request(
                "privacy.export_data",
                json!({
                    "limit_per_collection":500,
                    "include_audit":false,
                    "snapshot_at":snapshot_at,
                    "cursor":cursor
                }),
                "export-page-2",
            ),
            false,
        )
        .unwrap();
    assert_eq!(page["session_events"].as_array().unwrap().len(), 1);
    assert_eq!(page["has_more"]["session_events"], false);
    assert_eq!(page["has_more_any"], false);
}

#[test]
fn export_snapshot_excludes_events_written_after_the_first_page() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    for index in 0..3 {
        store
            .handle(
                &owner,
                &request(
                    "sessions.append_event",
                    json!({
                        "session_id":"snapshot-session",
                        "event_type":"user_message",
                        "payload":{"text":format!("before {index}")}
                    }),
                    &format!("snapshot-before-{index}"),
                ),
                false,
            )
            .unwrap();
    }
    let first = store
        .handle(
            &owner,
            &request(
                "privacy.export_data",
                json!({"limit_per_collection":2,"include_audit":false}),
                "snapshot-page-1",
            ),
            false,
        )
        .unwrap();
    store
        .handle(
            &owner,
            &request(
                "sessions.append_event",
                json!({
                    "session_id":"snapshot-session",
                    "event_type":"user_message",
                    "payload":{"text":"written during export"}
                }),
                "snapshot-during",
            ),
            false,
        )
        .unwrap();
    let second = store
        .handle(
            &owner,
            &request(
                "privacy.export_data",
                json!({
                    "limit_per_collection":2,
                    "include_audit":false,
                    "snapshot_at":first["snapshot_at"],
                    "cursor":first["next_cursor"]
                }),
                "snapshot-page-2",
            ),
            false,
        )
        .unwrap();
    let mut ids = first["session_events"]
        .as_array()
        .unwrap()
        .iter()
        .chain(second["session_events"].as_array().unwrap())
        .map(|row| row["event_id"].as_str().unwrap().to_owned())
        .collect::<Vec<_>>();
    ids.sort();
    ids.dedup();
    assert_eq!(ids.len(), 3);
}

#[test]
fn export_snapshot_is_per_collection_not_one_global_rowid() {
    let directory = TempDir::new().unwrap();
    let store = Store::open(directory.path().join("memory.db")).unwrap();
    let owner = identity();
    for index in 0..2 {
        store
            .handle(
                &owner,
                &request(
                    "sessions.append_event",
                    json!({
                        "session_id":"per-collection-session",
                        "event_type":"user_message",
                        "payload":{"text":format!("event {index}")}
                    }),
                    &format!("per-collection-event-{index}"),
                ),
                false,
            )
            .unwrap();
    }
    let first = store
        .handle(
            &owner,
            &request(
                "privacy.export_data",
                json!({"limit_per_collection":1,"include_audit":false}),
                "per-collection-page-1",
            ),
            false,
        )
        .unwrap();
    // A summary written after page 1 must not leak into page 2 even though its
    // rowid is below the max rowid of the event table.
    store
        .handle(
            &owner,
            &request(
                "sessions.update_summary",
                json!({
                    "session_id":"per-collection-session",
                    "patch":{"current_status":"done"},
                    "source_event_sequence":1,
                    "expected_revision":0
                }),
                "per-collection-summary",
            ),
            false,
        )
        .unwrap();
    let second = store
        .handle(
            &owner,
            &request(
                "privacy.export_data",
                json!({
                    "limit_per_collection":1,
                    "include_audit":false,
                    "snapshot":first["snapshot"],
                    "cursor":first["next_cursor"]
                }),
                "per-collection-page-2",
            ),
            false,
        )
        .unwrap();
    assert_eq!(second["session_summaries"].as_array().unwrap().len(), 0);
}
