use memory_store::{cleanup_migration_temporary_files, cleanup_plaintext_backups};
use tempfile::TempDir;

#[test]
fn cleanup_only_removes_database_scoped_plaintext_backups() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let stale = directory.path().join("memory.db.plaintext-backup-123");
    let unrelated = directory.path().join("other.plaintext-backup-123");
    std::fs::write(&stale, b"plaintext").unwrap();
    std::fs::write(&unrelated, b"keep").unwrap();

    assert_eq!(cleanup_plaintext_backups(&database).unwrap(), 1);
    assert!(!stale.exists());
    assert!(unrelated.exists());
}

#[test]
fn cleanup_only_removes_database_scoped_migration_temporaries() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let stale = directory.path().join(".memory.db.encrypted-interrupted");
    let unrelated = directory.path().join(".other.db.encrypted-interrupted");
    std::fs::write(&stale, b"encrypted staging").unwrap();
    std::fs::write(&unrelated, b"keep").unwrap();

    assert_eq!(cleanup_migration_temporary_files(&database).unwrap(), 1);
    assert!(!stale.exists());
    assert!(unrelated.exists());
}

#[cfg(unix)]
#[test]
fn cleanup_refuses_non_regular_migration_temporary() {
    let directory = TempDir::new().unwrap();
    let database = directory.path().join("memory.db");
    let target = directory.path().join("target");
    std::fs::create_dir(&target).unwrap();
    let stale = directory.path().join(".memory.db.encrypted-directory");
    std::os::unix::fs::symlink(&target, &stale).unwrap();

    let error = cleanup_migration_temporary_files(&database).unwrap_err();
    assert_eq!(error.code, "IntegrityError");
    assert!(stale.exists());
}
