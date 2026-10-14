use aes_gcm::aead::{Aead, KeyInit};
use aes_gcm::{Aes256Gcm, Nonce};
use argon2::Argon2;
use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;
use rand::rngs::OsRng;
use rand::RngCore;
use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::Path;
use zeroize::{Zeroize, Zeroizing};

const FORMAT: &str = "bianbu-agent-memory.key-package.v1";
const KDF: &str = "argon2id";
const CIPHER: &str = "aes-256-gcm";

#[derive(Debug, Deserialize, Serialize)]
struct KeyPackage {
    format: String,
    kdf: String,
    cipher: String,
    salt_b64: String,
    nonce_b64: String,
    wrapped_database_key_b64: String,
    created_at: i64,
    updated_at: i64,
}

pub fn read_secret_file(path: &Path) -> Result<Zeroizing<Vec<u8>>, Box<dyn std::error::Error>> {
    let metadata = fs::metadata(path)?;
    if metadata.permissions().mode() & 0o007 != 0 {
        return Err(format!(
            "secret file {} must not be accessible by group or others",
            path.display()
        )
        .into());
    }
    let mut secret = Zeroizing::new(fs::read(path)?);
    while secret
        .last()
        .is_some_and(|byte| matches!(byte, b'\n' | b'\r'))
    {
        secret.pop();
    }
    if secret.len() < 8 {
        return Err("account secret must contain at least 8 bytes".into());
    }
    Ok(secret)
}

pub fn read_capsule_key_file(
    path: &Path,
) -> Result<Zeroizing<Vec<u8>>, Box<dyn std::error::Error>> {
    let metadata = fs::metadata(path)?;
    if metadata.permissions().mode() & 0o007 != 0 {
        return Err(format!(
            "capsule key file {} must not be accessible by group or others",
            path.display()
        )
        .into());
    }
    let encoded = fs::read(path)?;
    let mut trimmed = encoded.as_slice();
    // postinst 用 dd/openssl rand 生成的密钥是 32 字节原始二进制，不带换行符：
    // 这种情况下任何一个字节（包括末尾的 0x0a/0x0d）都是密钥内容，不能当换行剥掉。
    // 只有长度不是 32 字节时才剥离尾部换行，保留旧版对「32 字节密钥 + 换行」这类
    // 文本写法的兼容，同时避免把合法二进制密钥截成 31 字节误入 base64 分支。
    if trimmed.len() != 32 {
        while trimmed
            .last()
            .is_some_and(|byte| matches!(byte, b'\n' | b'\r'))
        {
            trimmed = &trimmed[..trimmed.len() - 1];
        }
    }
    let key = if trimmed.len() == 32 {
        trimmed.to_vec()
    } else if trimmed.len() == 64 && trimmed.iter().all(|byte| byte.is_ascii_hexdigit()) {
        hex::decode(trimmed)?
    } else {
        BASE64.decode(trimmed)?
    };
    if key.len() != 32 {
        return Err("capsule key must contain exactly 32 bytes".into());
    }
    Ok(Zeroizing::new(key))
}

pub fn initialize(
    package_path: &Path,
    account_secret: &[u8],
) -> Result<Zeroizing<Vec<u8>>, Box<dyn std::error::Error>> {
    if package_path.exists() {
        return Err(format!("key package already exists: {}", package_path.display()).into());
    }
    let mut database_key = Zeroizing::new(vec![0_u8; 32]);
    OsRng.fill_bytes(&mut database_key);
    write_package(package_path, account_secret, &database_key, None)?;
    Ok(database_key)
}

pub fn unlock(
    package_path: &Path,
    account_secret: &[u8],
) -> Result<Zeroizing<Vec<u8>>, Box<dyn std::error::Error>> {
    let encoded = fs::read(package_path)?;
    let package: KeyPackage = serde_json::from_slice(&encoded)?;
    validate_package(&package)?;
    let salt = BASE64.decode(package.salt_b64)?;
    let nonce = BASE64.decode(package.nonce_b64)?;
    let ciphertext = BASE64.decode(package.wrapped_database_key_b64)?;
    if nonce.len() != 12 {
        return Err("key package nonce length is invalid".into());
    }
    let wrapping_key = derive_key(account_secret, &salt)?;
    let cipher = Aes256Gcm::new_from_slice(wrapping_key.as_ref())
        .map_err(|_| "unable to construct key-package cipher")?;
    let mut plaintext = Zeroizing::new(
        cipher
            .decrypt(Nonce::from_slice(&nonce), ciphertext.as_ref())
            .map_err(|_| "account secret cannot unlock the key package")?,
    );
    if plaintext.len() != 32 {
        plaintext.zeroize();
        return Err("unwrapped database key length is invalid".into());
    }
    Ok(plaintext)
}

pub fn rotate(
    package_path: &Path,
    old_secret: &[u8],
    new_secret: &[u8],
) -> Result<(), Box<dyn std::error::Error>> {
    let database_key = unlock(package_path, old_secret)?;
    let existing: KeyPackage = serde_json::from_slice(&fs::read(package_path)?)?;
    write_package(
        package_path,
        new_secret,
        &database_key,
        Some(existing.created_at),
    )
}

pub fn export(package_path: &Path, destination: &Path) -> Result<(), Box<dyn std::error::Error>> {
    let encoded = fs::read(package_path)?;
    let package: KeyPackage = serde_json::from_slice(&encoded)?;
    validate_package(&package)?;
    atomic_write(destination, &encoded)
}

fn write_package(
    package_path: &Path,
    account_secret: &[u8],
    database_key: &[u8],
    created_at: Option<i64>,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut salt = [0_u8; 16];
    let mut nonce = [0_u8; 12];
    OsRng.fill_bytes(&mut salt);
    OsRng.fill_bytes(&mut nonce);
    let wrapping_key = derive_key(account_secret, &salt)?;
    let cipher = Aes256Gcm::new_from_slice(wrapping_key.as_ref())
        .map_err(|_| "unable to construct key-package cipher")?;
    let ciphertext = cipher
        .encrypt(Nonce::from_slice(&nonce), database_key)
        .map_err(|_| "unable to wrap database key")?;
    let now = memory_core::now_ms();
    let package = KeyPackage {
        format: FORMAT.to_owned(),
        kdf: KDF.to_owned(),
        cipher: CIPHER.to_owned(),
        salt_b64: BASE64.encode(salt),
        nonce_b64: BASE64.encode(nonce),
        wrapped_database_key_b64: BASE64.encode(ciphertext),
        created_at: created_at.unwrap_or(now),
        updated_at: now,
    };
    atomic_write(package_path, &serde_json::to_vec_pretty(&package)?)
}

fn derive_key(
    account_secret: &[u8],
    salt: &[u8],
) -> Result<Zeroizing<[u8; 32]>, Box<dyn std::error::Error>> {
    let mut output = Zeroizing::new([0_u8; 32]);
    Argon2::default()
        .hash_password_into(account_secret, salt, output.as_mut())
        .map_err(|error| format!("Argon2id key derivation failed: {error}"))?;
    Ok(output)
}

fn validate_package(package: &KeyPackage) -> Result<(), Box<dyn std::error::Error>> {
    if package.format != FORMAT || package.kdf != KDF || package.cipher != CIPHER {
        return Err("unsupported key package format".into());
    }
    Ok(())
}

fn atomic_write(path: &Path, contents: &[u8]) -> Result<(), Box<dyn std::error::Error>> {
    let parent = path.parent().ok_or("key package path has no parent")?;
    fs::create_dir_all(parent)?;
    let temporary = path.with_extension(format!("tmp-{}", std::process::id()));
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(contents)?;
    file.sync_all()?;
    drop(file);
    fs::rename(&temporary, path)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    if let Ok(directory) = OpenOptions::new().read(true).open(parent) {
        let _ = directory.sync_all();
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn package_unlock_rotate_and_export() {
        let directory = TempDir::new().unwrap();
        let package = directory.path().join("memory.keypkg");
        let exported = directory.path().join("exported.keypkg");
        let created = initialize(&package, b"old-password").unwrap();
        assert_eq!(&*created, &*unlock(&package, b"old-password").unwrap());
        assert!(unlock(&package, b"wrong-password").is_err());
        rotate(&package, b"old-password", b"new-password").unwrap();
        assert!(unlock(&package, b"old-password").is_err());
        assert_eq!(&*created, &*unlock(&package, b"new-password").unwrap());
        export(&package, &exported).unwrap();
        assert_eq!(fs::read(package).unwrap(), fs::read(exported).unwrap());
    }

    fn write_key_file(directory: &TempDir, name: &str, bytes: &[u8]) -> std::path::PathBuf {
        let path = directory.path().join(name);
        let mut file = std::fs::OpenOptions::new()
            .create(true)
            .write(true)
            .mode(0o600)
            .open(&path)
            .unwrap();
        use std::io::Write;
        file.write_all(bytes).unwrap();
        path
    }

    #[test]
    fn capsule_key_binary_ending_in_newline_byte_is_kept_whole() {
        // `dd if=/dev/urandom bs=32 count=1` produces a raw 32-byte key with no
        // trailing newline. A key whose last byte happens to be 0x0a (or 0x0d)
        // must still be read as-is, not truncated to 31 bytes and misrouted into
        // the base64 branch (which fails with "Invalid symbol ...").
        let directory = TempDir::new().unwrap();
        for trailing in [0x0a_u8, 0x0d_u8] {
            let mut key = vec![0x42_u8; 31];
            key.push(trailing);
            assert_eq!(key.len(), 32);
            let path = write_key_file(&directory, "capsule.key", &key);
            let read = read_capsule_key_file(&path).unwrap();
            assert_eq!(&*read, &key);
        }
    }

    #[test]
    fn capsule_key_with_trailing_newline_still_reads_32_byte_key() {
        // Older behavior accepted a text file of exactly 32 key bytes plus a
        // trailing newline when the key's last byte is not itself 0x0a/0x0d.
        // That stays supported: only the newline is stripped and the 32 key
        // bytes are preserved.
        let directory = TempDir::new().unwrap();
        let key = [0x42_u8; 32];
        let mut content = key.to_vec();
        content.push(b'\n');
        let path = write_key_file(&directory, "capsule.key", &content);
        let read = read_capsule_key_file(&path).unwrap();
        assert_eq!(&*read, &key);
    }

    #[test]
    fn capsule_key_text_formats_still_decode() {
        let directory = TempDir::new().unwrap();
        let expected = [0x24_u8; 32];

        let hex = hex::encode(expected);
        let hex_path = write_key_file(&directory, "capsule.hex", hex.as_bytes());
        assert_eq!(
            &*read_capsule_key_file(&hex_path).unwrap(),
            &expected
        );

        let base64 = BASE64.encode(expected);
        let base64_path = write_key_file(&directory, "capsule.b64", base64.as_bytes());
        assert_eq!(
            &*read_capsule_key_file(&base64_path).unwrap(),
            &expected
        );
    }
}
