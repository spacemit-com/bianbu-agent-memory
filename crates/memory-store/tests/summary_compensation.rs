use memory_core::{capability, MemoryIdentity, PROTOCOL_VERSION};
use memory_protocol::RequestEnvelope;
use memory_store::Store;
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use tempfile::TempDir;
use uuid::Uuid;

fn owner() -> MemoryIdentity {
    MemoryIdentity {
        account_id: "summary-account".into(), device_id: "test-device".into(),
        agent_id: "test-agent".into(), agent_name: "test-agent".into(),
        agent_instance_id: "test-instance".into(), memory_space_id: "test-space".into(),
        capabilities: capability::ALL.iter().map(|v| (*v).to_owned()).collect(),
        peer_uid: 1000, peer_gid: 1000, peer_pid: 1, peer_start_time_ticks: 1,
    }
}

fn call(store: &Store, owner: &MemoryIdentity, method: &str, params: Value) -> Value {
    store.handle(owner, &RequestEnvelope {
        protocol: PROTOCOL_VERSION.into(), request_id: Uuid::new_v4().to_string(),
        trace_id: Uuid::new_v4().to_string(), method: method.into(), params,
        idempotency_key: Some(Uuid::new_v4().to_string()), deadline_ms: Some(5000),
    }, false).unwrap()
}

fn event(store: &Store, owner: &MemoryIdentity, sid: &str, kind: &str, close: bool) {
    call(store, owner, "sessions.append_event", json!({
        "session_id":sid, "event_type":kind,
        "payload":{"text":"Continue testing the task", "session_end":close, "run_id":"test-run"}
    }));
}

fn turn(store: &Store, owner: &MemoryIdentity, sid: &str, close: bool) {
    event(store, owner, sid, "user_message", false);
    event(store, owner, sid, "assistant_message", close);
}

fn context(store: &Store, owner: &MemoryIdentity, sid: &str) -> Value {
    call(store, owner, "sessions.get_context", json!({"session_id":sid,"include_summary":true}))
}

fn age_and_due(path: &std::path::Path, account: &str) {
    let db = Connection::open(path).unwrap();
    db.execute("UPDATE session_events SET created_at=created_at-120000 WHERE account_id=?1",
        params![account]).unwrap();
    db.execute("UPDATE background_worker_state SET next_retry_at=0", []).unwrap();
}

#[test]
fn idle_short_session_recovers_after_restart_without_runtime_or_raw_deletion() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("memory.db");
    let identity = owner();
    {
        let store = Store::open(&path).unwrap();
        turn(&store, &identity, "short", false);
        assert!(context(&store, &identity, "short")["summary"].is_null());
        // A new conversation is not a close signal for the old one.
        turn(&store, &identity, "other", false);
        store.run_background_jobs("test-device").unwrap();
        assert!(context(&store, &identity, "short")["summary"].is_null());
    }
    age_and_due(&path, &identity.account_id);
    let store = Store::open(&path).unwrap();
    let result = store.run_background_jobs("test-device").unwrap();
    assert_eq!(result["failed"], 0);
    let summary = context(&store, &identity, "short")["summary"].clone();
    assert_eq!(summary["source_event_sequence"], 2);
    let db = Connection::open(&path).unwrap();
    let count: i64 = db.query_row("SELECT COUNT(*) FROM session_events WHERE session_id='short'", [], |r| r.get(0)).unwrap();
    assert_eq!(count, 2);
    db.execute("UPDATE background_worker_state SET next_retry_at=0", []).unwrap();
    store.run_background_jobs("test-device").unwrap();
    assert_eq!(context(&store, &identity, "short")["summary"]["revision"], summary["revision"]);
}

#[test]
fn idle_tail_is_repaired_without_crossing_account_boundary() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("memory.db");
    let store = Store::open(&path).unwrap();
    let identity = owner();
    for _ in 0..5 { turn(&store, &identity, "tail", false); }
    assert_eq!(context(&store, &identity, "tail")["summary"]["source_event_sequence"], 10);
    turn(&store, &identity, "tail", false);
    let mut other = owner();
    other.account_id = "other-account".into();
    turn(&store, &other, "tail", false);
    age_and_due(&path, &identity.account_id);
    store.run_background_jobs("test-device").unwrap();
    assert_eq!(context(&store, &identity, "tail")["summary"]["source_event_sequence"], 12);
    assert!(context(&store, &other, "tail")["summary"].is_null());
}

#[test]
fn explicit_close_summarizes_short_session_only_after_completed_assistant() {
    let dir = TempDir::new().unwrap();
    let store = Store::open(dir.path().join("memory.db")).unwrap();
    let identity = owner();
    event(&store, &identity, "closed", "user_message", true);
    assert!(context(&store, &identity, "closed")["summary"].is_null());
    event(&store, &identity, "closed", "assistant_message", true);
    assert_eq!(context(&store, &identity, "closed")["summary"]["source_event_sequence"], 2);
}

#[test]
fn background_skips_in_progress_and_disabled_memory() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("memory.db");
    let store = Store::open(&path).unwrap();
    let identity = owner();
    turn(&store, &identity, "active", false);
    event(&store, &identity, "active", "task_started", false);
    age_and_due(&path, &identity.account_id);
    store.run_background_jobs("test-device").unwrap();
    assert!(context(&store, &identity, "active")["summary"].is_null());
    event(&store, &identity, "active", "task_failed", false);
    call(&store, &identity, "privacy.settings.update", json!({"memory_enabled":false}));
    age_and_due(&path, &identity.account_id);
    store.run_background_jobs("test-device").unwrap();
    call(&store, &identity, "privacy.settings.update", json!({"memory_enabled":true}));
    assert!(context(&store, &identity, "active")["summary"].is_null());
    age_and_due(&path, &identity.account_id);
    store.run_background_jobs("test-device").unwrap();
    assert!(context(&store, &identity, "active")["summary"]["revision"].as_i64().unwrap() > 0);
}

#[test]
fn malformed_legacy_sessions_cannot_starve_the_repair_batch() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("memory.db");
    let store = Store::open(&path).unwrap();
    let identity = owner();
    for i in 0..9 {
        // No matching user turn: retained audit rows must not form a summary.
        event(&store, &identity, &format!("legacy-{i}"), "assistant_message", false);
    }
    turn(&store, &identity, "valid", false);
    age_and_due(&path, &identity.account_id);
    store.run_background_jobs("test-device").unwrap();
    assert!(context(&store, &identity, "valid")["summary"].is_null());
    age_and_due(&path, &identity.account_id);
    store.run_background_jobs("test-device").unwrap();
    assert_eq!(context(&store, &identity, "valid")["summary"]["source_event_sequence"], 2);
}

#[test]
fn scheduler_migration_preserves_existing_retry_state() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("memory.db");
    {
        let store = Store::open(&path).unwrap();
        let identity = owner();
        turn(&store, &identity, "short", false);
        store.run_background_jobs("test-device").unwrap();
    }
    let db = Connection::open(&path).unwrap();
    // Simulate a v12 schedule with a retry worth preserving.
    db.execute("DELETE FROM background_worker_state WHERE job_kind='summary'", []).unwrap();
    db.execute("UPDATE background_worker_state SET retry_count=3, next_retry_at=123 WHERE job_kind='maintenance'", []).unwrap();
    db.execute("ALTER TABLE background_worker_state DROP COLUMN scan_offset", []).unwrap();
    // Keep the v14 marker: older device branches can carry a high marker
    // without the scheduler extension. Feature detection must still repair it.
    drop(db);
    let _store = Store::open(&path).unwrap();
    let db = Connection::open(&path).unwrap();
    let row: (i64,i64,i64) = db.query_row(
        "SELECT retry_count,next_retry_at,scan_offset FROM background_worker_state WHERE job_kind='maintenance'",
        [], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?))).unwrap();
    assert_eq!(row, (3,123,0));
}

#[test]
fn abandoned_task_watermark_does_not_block_completed_turns_forever() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("memory.db");
    let store = Store::open(&path).unwrap();
    let identity = owner();
    turn(&store, &identity, "abandoned", false);
    event(&store, &identity, "abandoned", "task_started", false);
    let db = Connection::open(&path).unwrap();
    db.execute("UPDATE session_events SET created_at=created_at-700000", []).unwrap();
    store.run_background_jobs("test-device").unwrap();
    // Summary may include the task watermark as an operation, never as a
    // completed user/assistant pair. L1 and the unfinished task remain intact.
    assert!(context(&store, &identity, "abandoned")["summary"]["revision"].as_i64().unwrap() > 0);
    let count: i64 = db.query_row("SELECT COUNT(*) FROM session_events", [], |r| r.get(0)).unwrap();
    assert_eq!(count, 3);
}
