# 贡献指南

[English](CONTRIBUTING.md) | [简体中文](CONTRIBUTING_CN.md)

请在 Pull Request 中说明问题、预期行为和验证结果。一次修改聚焦一个目标；行为变化时，同步更新文档和回归测试。

## 反馈问题

请提供源码提交号、软件包版本、操作系统、复现步骤、预期结果和实际结果。使用虚构的示例数据，不要在公开 Issue 中上传真实记忆导出文件、用户数据库、凭据、签名私钥或未经脱敏的对话日志。

## 版本说明

源码元数据版本与 Debian 软件包版本的关系见[版本说明](VERSIONING_CN.md)。

## 开发检查

当前未提供 GitHub Actions 自动验证，请执行下面的本地检查。内部 GitLab 流水线用于软件包构建与发布，不等同于 GitHub PR 自动验证。

当前未公布专门的私密漏洞报告渠道。请勿在公开 Issue 或 PR 中提交漏洞利用细节或敏感数据；请先联系项目维护者协调反馈方式，再提供详情。

记忆服务面向 Linux。请先按照 [中文 README](README_CN.md) 准备构建依赖，再执行：

```bash
python -m pip install -e 'sdk/python[test]'
cargo fmt --all -- --check
cargo test --workspace --locked
python -m pytest sdk/python/tests -q
```

K3、真实 Gateway、断电和长时间压测属于设备专项验证。未经设备负责人协调，不要在正在验收或压测的设备上运行这些检查。报告中应将设备专项验证与本地测试分开说明。

生产环境必须保留身份校验。示例和测试请使用文档介绍的隔离开发环境。
