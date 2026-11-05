# 版本说明

[English](VERSIONING.md) | [简体中文](VERSIONING_CN.md)

当前公开源码包含与 Debian 软件包 `bianbu-agent-memory`、`bianbu-agent-memoryd` 0.1.33 对应的功能。这里说明的是同步源码的基线，不代表本仓库已经创建对应的 GitHub Release 或发布软件包。

Cargo 工作区与 Python SDK 的元数据版本目前均为 0.1.6，不能将它们当作 Debian 发布版本。反馈问题时，请同时提供源码提交号和已安装的软件包版本。

公开仓库有独立的快照提交历史，内部发布 tag 与公开快照提交不一定使用相同的标识。公开快照保留脱敏后的示例，不包含内部 CI 配置。

不要根据 Cargo 或 Python 版本推断软件源中可安装的 Debian 版本，也不要为匹配发布包版本而修改 main 分支的 Debian changelog。Debian 发布由发行版流水线管理。
