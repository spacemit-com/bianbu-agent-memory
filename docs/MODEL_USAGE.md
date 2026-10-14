# AgentMemory 模型使用说明

本文说明记忆模块中各类模型的需求来源、数据流、配置方式和推荐选型。文中的“模型”
不是同一个组件：L2 摘要模型、Mem0 LLM 和 Embedding 分别承担不同职责，不能相互
替代。

## 1. 需求、实现与模型建议

| 需求 | 目前实现逻辑 | 推荐模型（结合 K3） |
| --- | --- | --- |
| 对话摘要和近期操作历史 | memoryd 每 5 轮或出现工具/命令/文件事件时生成规则摘要，保存 L2；可选调用 `summary_model` 优化摘要，失败时回退规则摘要 | K3 实测优先：`glm-edge-1.5b-chat-q4_0.gguf`；Qwen2.5-1.5B Q4_K_M 为可选候选。云端：Qwen2.5-7B/14B |
| 用户偏好、重要标记 | 规则提取明确事实，写入 SQLite；通过稳定槽位、revision 和冲突规则更新；可选 Mem0 LLM 抽取/合并 | K3 当前使用规则模式；端侧可测试 `Qwen2.5-3B-Instruct Q4_K_M`；云端：Qwen2.5-7B |
| 开发习惯 | 识别编程语言、依赖管理、常用命令等，写入 SQLite；Runtime 跨会话召回并应用 | K3 当前使用规则模式；端侧推荐 3B；云端推荐 Qwen2.5-7B |
| 长期记忆语义检索 | Embedding 将记忆和查询转换为向量，写入 Qdrant；召回后回查 SQLite 权威记录；索引不可用时回退 FTS5 | 端侧：`Qwen3-Embedding-0.6B Q8_0`；云端：平台统一中文 Embedding 服务，维度固定 |
| Mem0 自动抽取和合并 | `rules` 模式调用 Mem0 `infer=False`，当前已验证；`upstream` 模式调用 Mem0 `infer=True`，仍需准确率、延迟和内存验收 | K3 暂不建议启用 `infer=True`；端侧优先 3B，云端优先 7B |
| 云端模型 Fallback | 由 Runtime/Security/模型路由控制，Memory 不直接决定云端路由；需用户授权和隐私过滤 | 云端使用受控 OpenAI-compatible 服务；模型可选 Qwen2.5-7B/14B，端侧模型作为降级方案 |

L1 原始事件和 L2 摘要默认本地保存。模型不是记忆事实来源；SQLite canonical record
始终是权威数据，模型输出必须经过字段校验、身份命名空间、revision 和隐私策略检查。

## 2. 实现策略评估与采用建议

建议保持当前规则逻辑作为生产默认，把模型作为可选增强，不立即全面切换。

| 场景 | 建议 |
| --- | --- |
| L2 对话摘要 | 可优先试用模型，但必须异步调用；先保存规则摘要，模型成功后再优化，失败继续使用规则摘要 |
| L3 用户偏好和开发习惯 | 暂时保持规则抽取 + Mem0 `infer=False`，保证槽位更新、冲突处理和隐私边界的确定性 |
| Mem0 `infer=True` | 暂不作为生产默认；当前 K3 测试仍需解决中文语义误判、延迟和内存成本问题 |
| Embedding 语义检索 | 可以继续使用；它只负责向量检索，不负责事实判断，服务异常时回退 FTS5/Scheme A |

当前规则方案更适合 K3 长时间运行：延迟低、结果稳定、便于审计和回归测试，不依赖
额外模型服务，且模型或网络故障不会影响基本记忆功能。模型增强可以改善同义表达、
长对话重点、决策和待办提炼，但会增加 CPU、内存和延迟，并引入遗漏、幻觉、非法输出
和隐私数据出境等风险。

结合 K3，推荐按以下顺序推进：

1. 保持 `memoryd.rules` 和 Mem0 `infer=False` 为默认生产路径。
2. 先启用 L2 `summary_model` 做离线或异步对比测试。K3 当前实测优先使用
   `glm-edge-1.5b-chat-q4_0.gguf`；`Qwen2.5-1.5B-Instruct Q4_K_M` 已验证可用但端到端
   延迟存在抖动，质量或资源需求变化时再评估 Qwen 3B。
3. 完成固定中文事实集、否定表达、冲突偏好、非法输出、超时、P95 延迟和峰值内存
   测试后，再考虑启用 Mem0 `infer=True`。
4. 模型始终不能替代 SQLite canonical record、规则校验、revision 和用户确认机制。

最终采用“规则兜底 + 模型增强”的混合方案：L2 优先评估模型摘要，L3 Mem0 自动抽取
继续使用规则模式，直至 `infer=True` 通过完整质量和资源验收。

## 3. 模型分工总览

```text
Runtime/SDK 逐轮事件
       │
       ├─ memoryd.rules ───────────────► L2 结构化摘要（默认、无需 LLM）
       │                                  │
       │                                  └─ 可选 summary_model 改写摘要
       │
       └─ L3 投影任务 ─► Mem0 LLM（可选抽取/合并）
                              │
                              └─ Embedding ─► Qdrant 语义索引（可选）

查询 ─► Qdrant/Mem0 召回 ─► 映射回 SQLite canonical record ─► Runtime
                         （索引故障时回退 FTS5/Scheme A）
```

### 3.1 L2 摘要模型（memoryd `summary_model`）

L2 的默认实现是 `memoryd.rules`：每完成默认 5 个完整轮次，或出现新的工具/命令/文件
操作事件时，memoryd 从 L1 事件生成 `current_status`、决策、未完成事项、操作历史和
来源指针。规则摘要先提交，原始事件不会因为模型失败而丢失。

启用 `summary_model` 后，memoryd 在规则摘要提交之后调用一个 OpenAI-compatible
`POST /chat/completions` 服务。模型只能返回经过约束的摘要字段；返回非 200、超时、
非法 JSON 或字段不符合契约时，保留规则摘要并标记 `degraded_reason=model_unavailable`。

当前代码和状态：

- 配置结构由 `crates/memory-store/src/product.rs` 校验；默认 `enabled=false`。
- 调用实现位于 `crates/memory-store/src/sessions.rs`，不在 SQLite 写事务中推理。
- 当前 K3 未将 Qwen 作为正式默认服务；GLM 已完成本轮对比基线，规则模式仍是无模型时的可用兜底。

### 3.2 Mem0 LLM（D+ L3 抽取/合并）

Mem0 LLM 只处理 L3 候选事实的抽取和合并，不负责 L2 Session Summary。D+ sidecar
使用官方 `mem0ai==2.0.17`：

- `BIANBU_MEMORY_DPLUS_INFER_MODE=rules`（K3 当前默认）：`RuleBasedMem0Planner`
  仅提取明确的偏好、稳定事实和开发习惯，然后调用 Mem0 `add(..., infer=False)`；
- `BIANBU_MEMORY_DPLUS_INFER_MODE=upstream`：调用 Mem0 `add(..., infer=True)`，由
  Mem0 配置的 LLM 自动抽取/合并；
- 另有 SDK `OpenAICompatibleMem0Planner`，可通过 `BIANBU_MEMORY_LLM_*` 环境变量
  提供两阶段的 `facts` 抽取和 `ADD/UPDATE/DELETE/NONE` 合并契约。

无论使用规则还是 LLM，Memory 都会执行稳定槽位更新、旧 revision 失效、DELETE 用户
确认、敏感数据过滤和 SQLite canonical 回写。Mem0 返回的 provider 文本不能直接作为
Runtime 上下文。

当前状态：K3 的规则 + `infer=False` + 可选 Mem0/Qdrant 投影已验证；官方
`infer=True` 仍未通过中文准确率、延迟和内存门禁，不应标记为生产能力。

### 3.3 Embedding 模型

Embedding 将 L3 记忆和查询转换为向量。Memory 自研路径（MEM-NFR-02）下，向量
存储与召回在 memoryd 内实现：int8 量化向量存 SQLite `memory_embeddings` 表，
`long_term.search_headers` 内做暴力余弦扫描并与 FTS 做 RRF 融合，无外部向量库
依赖。D+ 引擎路径仍使用本地 Qdrant 做投影召回；两条路径共用同一个 Qwen3
Embedding 推理服务。Embedding 不负责摘要、事实抽取或冲突决策。向量和 Mem0 数据
都是可删除、可重建的派生数据；召回后必须按映射回查 SQLite 当前 revision、
命名空间和 Forget 状态。

Embedding 服务不可用时，L3 写入仍保留在 SQLite，memoryd 查询静默回退 FTS5
（MEM-NFR-02 降级要求），D+ 投影任务重试或进入失败状态；服务恢复后，
memoryd 后台任务自动补齐缺失/过期向量，D+ 可执行 projection rebuild。

## 4. 推荐模型

推荐优先采用支持 OpenAI-compatible API 的独立服务（K3 可使用 `llama.cpp`）。以下内存
和延迟是选型估算，必须在同一模型文件、推理引擎和上下文长度下实测。

### 4.1 端侧（K3）

| 用途 | P0 推荐 | P1/P2 备选 | 选择理由 |
| --- | --- | --- | --- |
| L2 摘要 | `Qwen2.5-1.5B-Instruct`，GGUF `Q4_K_M` | `Qwen2.5-3B-Instruct`，GGUF `Q4_K_M` | 中文摘要和资源占用的平衡；1.5B 质量不足再升 3B |
| Mem0 LLM 抽取/合并 | 暂用 `rules`，不启用 `infer=True` | `Qwen2.5-3B-Instruct`，GGUF `Q4_K_M` | L3 抽取需要比纯摘要更强的语义判断，仍需 K3 回归验收 |
| Embedding | `Qwen3-Embedding-0.6B`，GGUF `Q8_0` | 更大的 Embedding 模型 | 当前 K3 脚本和 1024 维配置已按此基线设计 |

端侧运行建议：

- 摘要模型作为独立服务运行，不在 memoryd 的 SQLite 事务内加载或推理；
- L2 初始资源预算为 4 CPU threads、4 GiB RAM、8192 context、512 output、15 秒超时；
- Qwen3 系列若用于摘要或 Mem0 LLM，必须关闭 thinking，避免延迟和非 JSON 输出；
- Memoryd、Runtime、D+ sidecar 和 Embedding 服务要分别测量 RSS、首 token、总延迟和
  并发影响，不能只看模型文件大小。

### 4.2 云端

Memory 当前没有把云端 LLM 作为必需依赖，也不会因为开启云端备份自动上传对话给模型。
云端模型调用必须由 Runtime/模型路由决定，并经过用户授权、网络白名单和敏感数据策略。

若平台已经提供受控的 OpenAI-compatible 云端端点，可按以下顺序选型：

| 用途 | 推荐起点 | 质量优先备选 | 备注 |
| --- | --- | --- | --- |
| L2 摘要 | Qwen2.5-7B-Instruct | Qwen2.5-14B-Instruct 或同等中文指令模型 | 端侧资源不足或需要更高摘要质量时使用；需限制上下文和输出 |
| Mem0 LLM 抽取/合并 | Qwen2.5-7B-Instruct | Qwen3-8B/14B（关闭 thinking，若服务支持） | 需验证 ADD/UPDATE/DELETE/NONE 结构化输出和事实保真 |
| Embedding | 平台统一的中文 Embedding 服务 | 更大维度模型 | 维度、距离函数和版本必须固定；更换模型后应重建 Qdrant |

这些云端模型只是推荐候选，不代表当前工程已完成云端模型验收。生产使用前至少要
完成中文事实/否定/冲突回归集、P95 延迟、超时降级、隐私字段过滤、成本和限流验证。

## 5. 配置示例

### 5.1 启用 memoryd L2 摘要模型

通过 Runtime Plugin Console 的 `settings.update` 配置，不直接编辑 SQLite：

```bash
curl -sS -X POST \
  http://127.0.0.1:9899/api/v1/plugin-consoles/bianbu.memory/actions/settings.update \
  -H 'Content-Type: application/json' \
  -d '{
    "summary_model": {
      "enabled": true,
      "base_url": "http://127.0.0.1:8080/v1",
      "model": "qwen2.5-1.5b-instruct-q4_k_m",
      "auth": "none",
      "credential_env": null,
      "max_context_tokens": 8192,
      "max_output_tokens": 512,
      "cpu_threads": 4,
      "ram_budget_mb": 4096,
      "max_latency_ms": 15000
    }
  }'
```

当前 memoryd 只接受受信任的 `http://` 摘要端点；云端生产端点的 HTTPS/TLS 由平台
适配器提供。需要鉴权时使用 `bearer_env` 或 `api_key_env`，`credential_env` 只写
环境变量名，禁止把 Token 写入配置文件。

### 5.2 启用 Mem0 LLM 抽取/合并

```bash
export BIANBU_MEMORY_ENGINE=dplus
export BIANBU_MEMORY_DPLUS_INFER_MODE=upstream
export BIANBU_MEMORY_DPLUS_LLM_BASE_URL=http://127.0.0.1:18790/v1
export BIANBU_MEMORY_DPLUS_LLM_MODEL=qwen2.5-3b-instruct-q4_k_m
export BIANBU_MEMORY_DPLUS_LLM_API_KEY=local
```

D+ sidecar 必须重新启动后读取这些环境变量。若继续使用当前已验证的规则模式：

```bash
export BIANBU_MEMORY_DPLUS_INFER_MODE=rules
```

### 5.3 Embedding 服务

Embedding 由 spacemit-ai-gateway 的 Embed 域统一提供，Memory 不再自带推理
unit 与模型下载脚本。装包会 enable 并后台触发一次 provisioner（下载 →
加载 → 金样自检）：

```bash
# 自动：装包即后台触发；gateway 不在时记一行日志并以 0 退出
sudo systemctl restart bianbu-agent-memory-embedding-provision.service

# 只查状态、不做变更
sudo /usr/libexec/bianbu-agent-memory-embedding-provision --check

# 直接看 gateway 侧的模型清单与状态
curl -sS http://127.0.0.1:18790/v1/embed/models
```

memoryd 原生语义检索（非 D+ 引擎路径）通过 `settings.update` 启用：

```bash
curl -sS -X POST \
  http://127.0.0.1:9899/api/v1/plugin-consoles/bianbu.memory/actions/settings.update \
  -H 'Content-Type: application/json' \
  -d '{
    "embedding": {
      "enabled": true,
      "base_url": "http://127.0.0.1:18790/v1/embed",
      "model": "qwen3-embedding-0.6b-q8_0",
      "dims": 1024
    }
  }'
```

注意 `model` 是 **gateway 侧的模型 id**，参与向量过期检测：更换 Embedding 模型
或维度后必须同步修改此值，旧向量会被判 stale 并由后台任务重建。

不要修改 gateway 的 `embed.backend` 与 `embed.default_args`，也不要调用
`/v1/embed/models/switch`：embed 域与 Agent OS 意图模块的 `bibit-embed`
共用，且有部署脚本会改写 `embed.default_args`。Memory 的模型改由 provisioner
用显式 `extra_args` 加载。详见 `docs/GATEWAY_EMBEDDING_INTEGRATION.md`。

D+ 引擎路径沿用环境变量：

```bash
export BIANBU_MEMORY_DPLUS_EMBEDDER_BASE_URL=http://127.0.0.1:18790/v1/embed
export BIANBU_MEMORY_DPLUS_EMBEDDER_MODEL=qwen3-embedding-0.6b-q8_0
export BIANBU_MEMORY_DPLUS_EMBEDDING_DIMS=1024
```

## 6. 验收与故障处理

至少覆盖：

1. 5 轮自动摘要、操作事件进入 L2、模型超时/4xx/非法 JSON 降级；
2. 用户偏好变更只保留当前有效槽位，开发习惯跨会话可召回；
3. 多 Agent 命名空间隔离，模型不能通过输出修改 `account_id` 或 `agent_id`；
4. Embedding/Qdrant 停止时 SQLite 写入、FTS5 检索和 Runtime 主流程仍可用；
5. 重启后 projection rebuild、索引版本变更和删除收敛；
6. 记录模型 P95 延迟、峰值 RSS、错误率、结构化输出通过率和事实准确率。

模型不可用时的正确结果是“保留原始事件并明确降级”，不是伪造摘要成功。若 L3 D+
投影失败，canonical 记忆仍可通过 FTS5/Scheme A 使用；若 L2 模型失败，
`memoryd.rules` 摘要继续提供基础能力。

## 7. 参考实现

- L2 摘要：`crates/memory-store/src/sessions.rs`、`docs/SUMMARY_MODEL_K3.md`
- 摘要配置校验：`crates/memory-store/src/product.rs`、`docs/CONFIGURATION.md`
- Mem0 LLM/D+：`sdk/python/src/bianbu_agent_sdk/memory/dplus.py`
- Mem0 规则和 SDK Planner：`sdk/python/src/bianbu_agent_sdk/memory/mem0.py`
- D+ HTTP sidecar：`sdk/python/src/bianbu_agent_sdk/memory/dplus_service.py`
- 适配边界和 K3 证据：`docs/MEM0_ADAPTATION.md`、`docs/IMPLEMENTATION_STATUS.md`
