# Fresh public source verification — 2026-11-05

[English](PUBLIC_BUILD_VERIFICATION_20261105.md) | [简体中文](PUBLIC_BUILD_VERIFICATION_20261105_CN.md)

Source: GitHub `spacemit-com/bianbu-agent-memory`, commit `2f8328d`. A fresh clone was built on a Linux riscv64 K3 with Rust/Cargo 1.93.1, Python 3.14.4 and OpenSSL 3.5.5. No installed service or package was replaced or restarted.

| Check | Result |
| --- | --- |
| `cargo build --workspace --release --locked` | Passed; cold build 11m 56s with two build jobs |
| `cargo test --workspace --locked` | 123 passed, zero failures |
| Fresh venv, `pip install -e 'sdk/python[test]'` | Passed |
| `python -m pytest sdk/python/tests -q` | 320 passed |
| README development instance | Write, search, canonical read and CLI health passed |

The development instance used a temporary database and independent socket and was stopped afterward. Health initially reported `busy`/`unknown`, matching the documented startup behavior. The production daemon PID and activation timestamp stayed unchanged.

The downloaded locked dependency manifests declare Rust 1.87 as their highest minimum (zbus and related crates). Rust 1.87 was not separately installed or built; the actual verified compiler is 1.93.1. Workspace metadata still declares 1.80, which is insufficient for this locked dependency set.

Compiler unused-code warnings and a pytest-asyncio fixture-loop-scope deprecation warning remain. They did not fail the commands. This check does not certify semantic recall quality, other architectures, GitHub-hosted CI execution, or production deployment.
