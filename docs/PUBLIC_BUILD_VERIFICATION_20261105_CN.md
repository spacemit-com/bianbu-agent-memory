# 公开源码全新构建验证 — 2026-11-05

[English](PUBLIC_BUILD_VERIFICATION_20261105.md) | [简体中文](PUBLIC_BUILD_VERIFICATION_20261105_CN.md)

源码来源：GitHub `spacemit-com/bianbu-agent-memory`，提交 `2f8328d`。在 Linux riscv64 K3 上全新克隆并构建，环境为 Rust/Cargo 1.93.1、Python 3.14.4 和 OpenSSL 3.5.5。未替换或重启已安装的服务或软件包。

| 检查项 | 结果 |
| --- | --- |
| `cargo build --workspace --release --locked` | 通过；使用两个构建任务，首次构建耗时 11 分 56 秒 |
| `cargo test --workspace --locked` | 123 项通过，零失败 |
| 全新虚拟环境中执行 `pip install -e 'sdk/python[test]'` | 通过 |
| `python -m pytest sdk/python/tests -q` | 320 项通过 |
| README 中的开发实例 | 写入、检索、权威记录读取及 CLI 健康检查均通过 |

开发实例使用临时数据库和独立 socket，验证后已停止。健康状态最初返回 `busy`/`unknown`，与文档说明的启动行为一致。生产守护进程的 PID 和启动时间均未变化。

已下载的锁定依赖清单中，声明的最低 Rust 版本最高为 1.87（zbus 及相关 crate）。未单独安装 Rust 1.87 或使用它构建；实际验证的编译器版本为 1.93.1。工作区元数据仍声明 1.80，该版本不足以构建当前锁定的依赖集合。

构建仍有未使用代码警告，测试仍有 pytest-asyncio fixture 事件循环作用域的弃用警告，但均未导致命令失败。本次检查不代表已验证语义召回质量、其他架构、GitHub 托管 CI 执行或生产部署。
