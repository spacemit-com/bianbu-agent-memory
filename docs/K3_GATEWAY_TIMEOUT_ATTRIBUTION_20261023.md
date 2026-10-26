# K3 Gateway 偶发 Embedding 超时归因（2026-10-23）

本文记录正式修复前的归因实验；文末“没有修改/提交生产源码”仅指该实验阶段。后续源码修复、独立正式复测与 Runtime 回归见 `K3_FINAL_PERFORMANCE_20261023.md`。

## 结论

本轮复现的偶发超时，直接原因是 **Gateway Python 垃圾回收阻塞事件循环**。Gateway 每个 Embedding 请求新建 `httpx.AsyncClient`，默认同时创建 TLS 上下文；请求结束后虽调用 `aclose()`，相关对象仍会进入循环垃圾回收。批量回收时出现秒级停顿，Memory 在 1.5 秒 I/O 超时后按既有策略回退关键词检索。

通过只复用 TLS 上下文、保留逐请求客户端创建/关闭的可撤销实验，3200 次负载降级从 24 次降为 0；恢复原逻辑后再次出现 24 次降级及 3.98 秒 GC 停顿。另用全新 Python 进程与本地 HTTP 桩复现回收开销差异，支持将修复放在 Gateway 转发适配器，而不是调整 Memory 超时。

该结论针对本轮复现，不代表所有网络超时都只可能来自 GC；没有关闭 GC、TLS 校验、语义检索或减少扫描数据。

## 环境及保护边界

- 设备 192.0.2.11；Gateway 软件包 0.0.19，Python 3.14.4；Runtime 与 Gateway 保持运行，filesearch 保持此前授权的停止状态。
- 正式 Gateway 18790；记忆模型 Q8 在 18802，意图模型在 18800。三者 PID 全程保持 `1052042/1066967/1052799`，未重启或改启动参数。
- Memory 采用缓存加载优化候选二进制，但只运行隔离 `development-identity` 栈，未覆盖生产二进制。
- 同一隔离数据库：10 万条记录、10 万条 1024 维合成存储向量，真实 Gateway 查询 Embedding；8 并发 × 400 个不同查询，每组 3200 次，查询序列相同。
- 语料只用于规模/性能，不用于评估真实语义召回质量。记录请求墙钟，不以测试期间 load1<3 为门禁。压测本身会增加负载，三组启动 load1 为 3.31/3.93/9.35，不能认为 CPU 调度状态完全一致。
- 临时 Python 计时与实验通过 3.14 `sys.remote_exec` 注入，不修改已安装文件；带自动撤销与主动停止机制。未记录生产聊天内容、凭证或用户记忆正文。

## 调用链与源码位置

```text
Memory semantic_stage
  → Gateway /v1/embed/embeddings
  → embed/api.py::_proxy
  → BaseModelService.proxy / _resolve_model / _do_load
  → LlamaEmbedAdapter.proxy
      → 每请求 httpx.AsyncClient(timeout=None)
          → 默认创建 TLS 上下文（即使请求地址为本机 HTTP）
      → client.send → 18802 模型 → 读取响应
  → response.aclose / client.aclose
  → 返回 Memory

累计请求中的循环对象被 GC 回收
  → GC 在 Gateway 事件循环线程执行
  → 同时阻塞其他请求接收、转发和响应处理
  → Memory 等待超过 1.5 秒，回退词法检索
```

部署文件位于 `/opt/spacemit-ai-gateway/venv/lib/python3.14/site-packages/`：

- `spacemit_ai_gateway/domains/embed/adapters/llama.py` 的 `LlamaEmbedAdapter.proxy`：每请求新建客户端。
- `spacemit_ai_gateway/domains/embed/api.py` 的 `_proxy`：正常响应后关闭 response/client；关闭网络客户端不等于所有 Python 循环对象立即回收。
- `httpx/_config.py::create_ssl_context`：默认 `verify=True` 会创建并加载受信证书上下文。

本地对应源码为 `ai-gateway-src/src/spacemit_ai_gateway/domains/embed/adapters/llama.py`，定位时仓库 HEAD `530aa47`，工作区未修改。该源码树不等同于已安装软件包的完整版本基线，正式改动前应同步 Gateway 团队仓最新代码。

## 原逻辑 → 复用上下文 → 恢复原逻辑

| 对照组 | 查询数 | search P95 / P99 | 最大查询耗时 | 接口错误 | 语义降级 |
|---|---:|---:|---:|---:|---:|
| A：原逻辑，业务/GC分段探针 | 3200 | 636.731 / 1257.108ms | 2817.145ms | 0 | 24 |
| B：仅18802复用TLS上下文 | 3200 | 552.468 / 625.284ms | 963.120ms | 0 | 0 |
| A′：撤销实验、恢复原逻辑 | 3200 | 623.516 / 1425.786ms | 2887.719ms | 0 | 24 |

三组有一致 GC/事件循环观测，但 A 额外记录业务方法与客户端初始化计时；B 的 GC 探针在该轮开始后约 95 秒才安装，故不能声称覆盖 B 的完整 GC 时间序列。Memory 侧完整 trace 确认 B 全部 3200 次没有 Embedding 降级。三组进程内存/GC 历史及调度状态也非完全一致，因此不把所有 P95 差值归为单一因素；A′重现和后述新进程独立复现共同增强因果判断。

关键时间对齐（设备 Unix 时间，GC时间为结束时刻）：

| 原逻辑窗口 | GC耗时 | 事件循环停顿 | GC回收对象数 |
|---|---:|---:|---:|
| A，1792749991.356 | 3753.952ms | 3753.743ms | 98692 |
| A，1792750047.266 | 1746.575ms | 1765.856ms | 46528 |
| A′，1792750635.803 | 3975.657ms | 3965.560ms | 105351 |
| A′，1792750691.512 | 1807.812ms | 1801.912ms | 46943 |

例如 A 第一窗口 GC 从约 1792749987.602 持续到 1792749991.356，其间 Memory 多个独立请求在约 1508～1529ms 超时，直到 GC结束后仍有受排队影响的后续超时。Gateway 内 `AsyncClient.send` 同期墙钟延迟 3.8 秒左右，不应误读为模型计算了 3.8 秒。前轮模型 slot 日志的任务间空档也与这一机制一致。

B 仍发生 GC：已观测到 533.980ms（回收 102946 个对象，可能包含此前积累对象）、95.734ms、81.372ms、33.256ms 等，说明实验没有关闭 GC。单轮零降级不保证生产永不超时。

## 不经过模型的独立复现

在设备上分别启动全新 Python 进程，使用同一 Gateway venv 的 HTTPX 与本机临时 HTTP 桩，执行 1200 次完整请求。完全不调用 Gateway/模型，排除模型推理与模型排队。

| 1200次请求 | 默认逐请求TLS上下文 | 复用TLS上下文 |
|---|---:|---:|
| 请求循环总耗时 | 34648ms | 6450ms |
| 最大单次GC | 886.007ms | 46.012ms |
| 进程峰值RSS | 312980KiB | 41612KiB |

该桩不包含完整 Gateway 路由，不能作为正式吞吐结果；它验证了反复创建上下文对请求路径的分配/回收成本，不能把峰值RSS直接叫作永久内存泄漏。

另做过“只创建/关闭1200个客户端、不发HTTP请求”的对照：默认约25.3秒，复用上下文约0.5秒，但两组GC最大均约25ms，**该简化实验未复现秒级GC**。因此需要结合完整HTTP请求路径解释循环对象回收，不把纯客户端构造误当作完整根因复现。

## 建议的正式修复

优先修改 Gateway 的 `LlamaEmbedAdapter`，为每个适配器惰性创建并复用一个 `httpx.create_ssl_context()`，创建客户端时传入 `verify=context`：

```text
adapter首次请求 → 按原默认信任策略创建SSLContext
后续请求       → AsyncClient(timeout=None, verify=该context)
               → 原有send、响应处理、关闭流程
```

现场实验只作用于18802；正式源码应按适配器实例管理资源，不能把18802端口硬编码成产品逻辑。保留证书验证、hostname检查和默认CA/env规则，不用 `verify=False`，不改变模型生命周期、权限或Memory超时。信任配置/CA轮换时需要明确上下文重建时机。

此方案保留现有逐请求 client 所有权，减少修改面。进一步复用整个 AsyncClient 可能改善连接池和分配成本，但需要同时设计共享client关闭、并发、异常、重载/卸载与服务退出，不建议为本次问题顺带扩大范围。暂停GC、调高GC阈值、绕过Gateway直连模型、延长Memory超时均不是本轮建议。

正式开发验证应覆盖：上下文复用及仍启用校验、并发转发/响应关闭、转发异常清理、加载/卸载、18800与18802共存，再按相同数据负载多轮复测并补Runtime聊天。实验结果足以支持这个修复方向，尚不等于已完成Gateway正式源码修复或发包。

## 清理与证据

全部实验已撤销，`gateway-probes-restored.json` 返回 `restored=true`：转发、HTTPX init/send、服务入口恢复site-packages原函数，无诊断GC callback残留。memoryd、Runtime、Gateway均active，Runtime `AGENT_READY`，意图模型与记忆模型PID未变。生产Memory仍是软件源二进制。

本轮没有修改/提交 Gateway 或 Memory 的生产源码，没有打tag。报告仅是诊断记录。

设备证据目录 `/home/example/memory-acceptance-20261023`；本地归档 `K3验收记录/20261023_192.0.2.11/gateway-timeout-attribution/`：三组 `gateway-*-attribution.json/.trace/.log`、`gateway-live-timing.jsonl`、`gateway-gc-trace.jsonl`、实验安装/撤销日志、恢复验证、HTTP桩与构造对照及原始输出。现场临时探针只做定位，不建议直接纳入生产。
