//! Stamp the daemon with the version recorded in `debian/changelog`.
//!
//! `debian/changelog` is the release source of truth for this package: the
//! packaging pipeline derives both the `.deb` version and the release branch
//! name from it, while `Cargo.toml` is not touched on release.  Reading it
//! here keeps `bianbu-agent-memoryd --version` in step with the package
//! instead of drifting at whatever the workspace version happened to be.
//!
//! Falls back to `CARGO_PKG_VERSION` when the changelog is unavailable (for
//! example a source archive that ships without `debian/`).

use std::path::Path;

fn main() {
    let changelog = Path::new("../../debian/changelog");
    println!("cargo:rerun-if-changed={}", changelog.display());
    if let Some(version) = changelog_version(changelog) {
        println!("cargo:rustc-env=BIANBU_AGENT_MEMORY_VERSION={version}");
    }
}

/// First line of the changelog looks like
/// `bianbu-agent-memory (0.1.15) unstable; urgency=medium`.
fn changelog_version(path: &Path) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let header = text.lines().next()?;
    let open = header.find('(')? + 1;
    let close = open + header[open..].find(')')?;
    let version = header[open..close].trim();
    if version.is_empty() || !version.starts_with(|c: char| c.is_ascii_digit()) {
        return None;
    }
    Some(version.to_owned())
}
