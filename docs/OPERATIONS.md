# Agent Memory 运行诊断

本文用于现场排查，不是配置项清单。可写配置、默认值和设置入口见
[配置指南](CONFIGURATION.md)。

## health 完整性检查

`system.health` 的 `status` / `database` 来自一次独占的 `PRAGMA quick_check`。整库
检查在 K3 现网库上约 406 ms，且该库是 SQLCipher：**索引完好时它也会报**
`malformed inverted index for FTS5 table main.long_term_fts[2]`，实测在写入高峰后
仍会持续复现约 100 s（连续 15 次取样）随后自行消失，之后不再出现，检索全程正确。
因此 0.1.18 起，health 只在“安静、WAL 为空、与写者互斥”时取样，并且**那条判词
不再算判词**：它以 `database_check.complaint` 原文呈现，不参与 `status`。

两个进程级环境变量见[配置指南 §8](CONFIGURATION.md)：
`AGENT_MEMORY_HEALTH_SETTLE_MS` 与 `AGENT_MEMORY_HEALTH_CHECK_INTERVAL_MS`。

响应字段：

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `status` | `ok` / `degraded` / `unknown` | 只由最近一次**已发布**的判词推导；FTS5 那条判词、写者占库都不改变它 |
| `database` | `ok` / 缺陷描述 / `busy` / `unknown` | 判词本体；`busy` = 本次没测到（写者占库或刚写过），`unknown` = 还没有取过样 |
| `database_check.state` | `checked` / `cached` / `busy` / `complaint` | 本次判词的来源：刚取样 / 复用缓存 / 没测到 / 有在案的投诉 |
| `database_check.reason` | `null` / `recent-activity` / `database-busy` / `fts5-inverted-index` | 为什么是现在这个 `state` |
| `database_check.verdict` | `ok` / 缺陷描述 / `null` | 已发布的判词 |
| `database_check.complaint` | `null` / 缺陷描述 | 不当作判词的投诉原文（目前只有 FTS5 inverted-index 那一类） |
| `database_check.checked_at_ms` · `age_ms` · `interval_ms` · `attempts` | 数值 | 判词时间戳、年龄、缓存窗口、本次独占检查尝试次数 |
| `embedding` | 对象 | 语义检索**当前生效**的配置：`enabled` / `base_url` / `model` / `dims`，以及 `config_file_matches`（磁盘上的 `memory.config.json` 是否已与本进程一致，见[配置指南 §5](CONFIGURATION.md)） |

`degraded` 只在“`quick_check` 报了不是 FTS5 inverted-index 的缺陷”时出现。监控规则
请针对 `degraded` 触发，不要用 `status != "ok"`（`unknown` 是合法取值：可能是当前有
写在并发、本次没测到，也可能是还没取过样）。若想观察那条瞬时判词而不告警，看
`database_check.state == "complaint"` 与 `database_check.complaint`。细节与实测见
`docs/GATEWAY_EMBEDDING_INTEGRATION.md` §8.2。

`health.embedding` 存在的理由是“磁盘副本与生效值错位”：升级到 0.1.17 的设备上，文件里
可能仍写着 0.1.16 的 `18801` endpoint，而服务已经在打 gateway。`config_file_matches`
为 `false` 说明这一份文件还没被写回（多为只读目录或外部改过文件），排查时以
`base_url`/`model` 为准。

## 命名空间清册

另一条排查入口是只读全库清册 —— 协议里每个方法只作用于调用者自己的 memory space，
所以“某个退役测试空间里还剩什么”只能由守护进程自己回答：

```bash
sudo /usr/libexec/bianbu-agent-memoryd --report-namespaces \
  --database /var/lib/bianbu-agent-memory/memory.db \
  --key-package /var/lib/bianbu-agent-memory/memory.keypkg \
  --account-secret-file /etc/bianbu-agent-memory/account-secret
```

输出每个 namespace 的逐表行数与正在使用的 `slot:` 标签（不含记忆正文）。它以
`SQLITE_OPEN_READ_ONLY` 打开库，不迁移、不改写、不加写锁，因此可以在服务运行期间执行；
覆盖脚本为 `scripts/k3_namespace_inventory.py`（清册 + 槽位合规）与
`scripts/k3_memory_namespace_cleanup.py`（按同一判据收口），用法与现场结果见
`docs/TEST_CASES.md` §4.3。
