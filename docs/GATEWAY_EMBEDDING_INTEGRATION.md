# Memory 语义检索接入 spacemit-ai-gateway 实施方案

状态：已实施，K3 实测验证通过（V1.2，2026-09-22）

适用设备：K3（Bianbu，rv64gcv），spacemit-ai-gateway 0.1.15

## 1. 背景与目标

Memory 的语义检索当前由 memoryd 自管一整套推理栈：

- `packaging/bianbu-agent-memory-download-embedding-model` 负责下载并校验
  Qwen3-Embedding-0.6B-Q8_0 GGUF；
- `packaging/bianbu-agent-memory-embedding.service` 拉起一个常驻 llama-server
  （`127.0.0.1:18801`）；
- memoryd 通过 `EmbeddingClient`（`crates/memory-store/src/embedding.rs`）以
  OpenAI 兼容的 `/embeddings` 协议消费该服务。

K3 上已经常驻 spacemit-ai-gateway（`127.0.0.1:18790`），其 Embed 域提供了完整的
模型生命周期接口（下载 / 安装 / 注册 / 加载 / 切换 / 检索代理）。本方案把上面三项
职责整体移交给 gateway，Memory 侧只保留"配置一个 endpoint + 调用"。

目标：

1. 模型下载、安装、加载、卸载、状态查询全部走 gateway 的 `/v1/embed/*` 接口；
2. 向量生成走 gateway 的 `/v1/embed/embeddings`（或 OpenAI 兼容 `/v1/embeddings`）；
3. memoryd 不再自带推理 unit 与下载脚本，也不再持有模型副本；
4. **不影响 Agent OS 意图模块（bibit-intent）正在使用的 `bibit-embed` 链路**。

## 2. 意图模块（bibit-intent）的既有契约

以下为 2026-09-22 在 K3 上实测确认，是本方案必须守住的边界。

运行中的意图进程环境变量：

```text
BIBIT_INTENT_INFERENCE_BACKEND=ai_gateway
BIBIT_INTENT_GATEWAY_URL=http://127.0.0.1:18790
BIBIT_INTENT_GATEWAY_LLM_MODEL=bibit-llm
BIBIT_INTENT_GATEWAY_EMBED_MODEL=bibit-embed
BIBIT_INTENT_GATEWAY_EMBED_REVISION=1
BIBIT_INTENT_GATEWAY_API_KEY=
```

关键事实：

- 意图模块**经 18790 网关访问**，不直连模型端口，且**显式传 `model=bibit-embed`**。
  因此 embed 域的 `_current_model` 指针变化对它无影响。
- `bibit-embed` 是 `local_path` 类型的非预置模型，指向
  `~/.cache/models/embed/Qwen3-Embedding-0.6B-Q4_0.gguf`，当前由端口 18800 上的
  `llama-server --embedding --threads 8 --metrics` 提供服务。
- 意图模块用 `BIBIT_INTENT_GATEWAY_EMBED_REVISION` 做自己的索引版本管理，说明它预期
  会更换 embed 模型 —— 这反过来说明 Memory 绝不能与之共用同一个模型注册项。

### 2.1 配置所有权冲突（重要）

`/opt/bibit-intent/deploy/configure-gateway-embedding.py` 会**改写 gateway 生效配置里的
`embed.default_args`**，目标值为：

```text
--embedding --pooling mean --embd-normalize 2 --ctx-size 2048 --threads 4 --cache-ram 0
```

该脚本会保留 `base.yaml.pre-bibit-embedding` 备份。而当前生效的
`/opt/spacemit-ai-gateway/configs/base.yaml`（2026-09-22 14:46 更新，晚于该备份）是
`--embedding --threads 8 --metrics`，与运行中的 18800 进程一致 —— 即 bibit 期望的参数
当前并未生效。

结论：`embed.default_args` 是一个**会被第三方 deploy 随时改写的全局共享项**。
Memory 若依赖它，随时可能在一次 bibit 重新部署后以"维度相同、语义不同"的向量静默污染
索引。因此本方案要求 Memory 的模型**在加载时用 `extra_args` 显式钉死全部关键参数**。

## 3. 四条红线（不碰意图模块）

1. 不改 `embed.backend`、不改 `embed.default_args`、不改 `port_pool`
   （`port_pool` 是全局单例并由 llm 段初始化，动它会把 bibit 的模型端口一起挪走）。
2. 不调用 `POST /v1/embed/models/switch`，不改动 `bibit-embed` 的注册记录。
3. Memory 的推理请求**始终显式带 `model=<自己的模型 id>`**。走 `_resolve_model` 的
   显式分支时，仅当 `_current_model` 为空才会更新指针，因此不会抢走 bibit 的当前模型。
4. 保持 `auth.enabled: false`。memoryd 的 `EmbeddingClient` 是手写 HTTP/1.1 客户端，
   不带任何鉴权头；意图模块的 `BIBIT_INTENT_GATEWAY_API_KEY` 也是空。

## 4. 为什么 Memory 使用独立模型而不是复用 `bibit-embed`

直觉上复用同一个 llama-server 最省内存，但实测与代码分析都指向独立模型：

| 维度 | 复用 `bibit-embed`（Q4_0） | 独立 `qwen3-embedding-0.6b-q8_0` |
| --- | --- | --- |
| 启动参数控制权 | 归 bibit 的 deploy 脚本，随时可能变成 `--pooling mean --ctx-size 2048` | 由 Memory 的 `extra_args` 钉死 |
| 参数被改后的后果 | 维度仍是 1024，memoryd 无法察觉 → 索引静默污染 | 不适用 |
| `--ctx-size 2048` | memoryd 的 2560 token 文本会开始吃 HTTP 400 | 无 |
| 向量一致性 | 与现网 Q8 链路余弦仅 0.53~0.87，需重建索引 + 重做质量验收 | 与现网链路余弦 **1.0000** |
| 权重体积差 | — | 仅 222 MB（382 MB → 604 MB） |

## 5. 实测证据（2026-09-22，K3 192.0.2.10）

- gateway embed 域可用：`POST /v1/embed/embeddings` 与兼容路由 `POST /v1/embeddings`
  均返回 1024 维，批量正常；热态延迟 0.06 s（旧 18801 链路 0.03 s），proxy 跳转对
  1.5 s / 2 s 检索预算可忽略。
- 下载生命周期可用：`POST /models/{id}/download` → `GET` 进度 7.5% / 15.7% / 23.6% /
  31.7% → `DELETE` 取消 → 状态回到 `available`；archive 侧
  `content-length: 639150592`，与 memoryd 旧下载脚本钉住的 size 一致。
- **向量一致性**：gateway 加载 `qwen3-embedding-0.6b-q8_0`（默认参数，未加 `--pooling`）
  对同一组文本与现网 18801（Q8_0 + `--pooling last` + ctx4096 + parallel1）余弦为
  `[1.0, 1.0, 1.0, 1.0]`。即 ctx-size / parallel / pooling 标志在本模型的 GGUF 上都不改变
  向量语义，**量化才是唯一敏感项**。
- `extra_args` 生效：以 `--ctx-size 4096 --parallel 1 --pooling last` 加载同样得到 1.0。
- 资源：gateway embed 服务默认 `n_ctx=32768 × 4 slots`，RSS 4.5 GB；指定
  `--ctx-size 4096 --parallel 1` 可与 memoryd 旧服务对齐（RSS 2.0 GB）。
- 并发：8 路并发请求全部成功（0.43 s）。`docs/CONFIGURATION.md` 记录的多 slot
  Spacemit 后端报错在当前 build `b10050` 未复现，但仍建议保守配置。
- 已知缺陷：`POST /models/deregister` 实测有一次未回收 llama-server 进程（残留占用
  18803 端口），已手工清理。属 gateway 侧问题，建议上报。
- 加载是**幂等**的：进程已在运行时 `POST /models/load` 只返回成功、不重启进程。
  因此模型被推理路径自动加载后（`_resolve_model` 用 `embed.default_args`），
  再发一次 `load` 不会把参数改回钉死的取值 —— 必须先 `unload`。这条实测直接
  决定了 provisioner 的写法（见 7.3）。
- **参数钉死的收益可量化**：同一份 Q8_0 GGUF，钉死参数的进程 RSS
  **1.72~1.78 GB**；被自动加载（只有 gateway 默认 `--embedding --threads 8
  --metrics`）后 RSS **4.98 GB**，差约 3.2 GB。
- `POST /models/unload` 只停指定模型（0.33 s 返回），期间 `bibit-embed` 的
  `/embeddings` 请求照常成功；随后一个指名该模型的请求会在 **6.3 s** 内自动加载
  并正常返回。即"模型被卸载"不等于"服务中断"，只是冷启动一次。
- provisioner 端到端（2026-09-22）：`--check` 退出 0；正式运行
  `下载 → unload(已在运行) → load(钉死参数) → 金样自检` 全绿，
  `parity: worst cosine 1.000000 over 6 texts`，退出 0，加载后进程回到
  1.78 GB。

## 6. 目标架构

```text
                     ┌──────────────────────────────────────────┐
  意图模块 ─────────► │  spacemit-ai-gateway  127.0.0.1:18790    │
  (bibit-intent)     │                                          │
  model=bibit-embed  │  Embed 域                                │
                     │   ├─ bibit-embed        → :18800 (Q4_0)  │ ← 原样不动
  memoryd ─────────► │   └─ qwen3-embedding-0.6b-q8_0 → :1880x  │ ← 本方案新增
  model=qwen3-...    │        (Q8_0, ctx4096, parallel1)        │
                     │                                          │
                     │  /v1/embed/models  /download  /load ...  │
                     └──────────────────────────────────────────┘
                                  ▲
                                  │ 仅生命周期调用，不 switch
                     bianbu-agent-memory-embedding-provision
```

- memoryd 仍然只做一件事：按配置的 `base_url` + `model` 发 `/embeddings` 请求。
- 新增的 provisioner 负责"确保模型已下载并以钉死的参数加载"。
- 运行期与安装期的逐步展开、以及「下载 / 加载 / 推理分别由谁承担」的归属见 §11。

## 7. 实施步骤

### 7.1 gateway 侧

**零配置文件改动。** 仅依靠预置模型 `qwen3-embedding-0.6b-q8_0`（已存在于
`/opt/spacemit-ai-gateway/configs/base.yaml` 的 `embed.models` 清单）与 `/v1/embed/*` 接口。

前置标定（一次性，实施时执行）：在设备上用候选 `--embd-normalize` 取值加载模型，
与现网基线向量比对，选出余弦 1.0 的取值后写进 provisioner。`--pooling last` 已确认
可与基线逐位一致。

### 7.2 memoryd 侧

1. `EmbeddingConfig::default()` 的 `base_url` 改为 `http://127.0.0.1:18790/v1/embed`，
   `model` 改为 `qwen3-embedding-0.6b-q8_0`。客户端代码零改动：`EmbeddingClient::new`
   已把 `{base}/embeddings` 拼成 `/v1/embed/embeddings`。
2. 删除自管推理栈：
   - `packaging/bianbu-agent-memory-embedding.service`
   - `packaging/bianbu-agent-memory-download-embedding-model`
   - `debian/rules` 中对应的两条 install
   - `debian/bianbu-agent-memoryd.postinst` 中的后台下载 / 拉起 unit / 创建 models 目录
   - `debian/bianbu-agent-memoryd.prerm` 中的 embedding unit 停止逻辑
3. `packaging/bianbu-agent-memoryd.service` 去掉对 embedding unit 的软依赖
   （原 `Wants=` / `After=` 一并删除，理由见 7.3：新 provisioner 首次运行可能要
   下载 ~640 MiB，不能把守护进程排在它后面）。gateway 不可用时行为不变：
   静默降级为词法检索。

### 7.3 provisioner

`packaging/bianbu-agent-memory-embedding-provision`（Python 3 标准库，不引入新
依赖）+ `packaging/bianbu-agent-memory-embedding-provision.service`（oneshot）。
逻辑：

1. `GET /v1/embed/healthz` —— gateway 不在则记一行日志并以 0 退出（非致命）；
2. `GET /v1/embed/models` —— 找到目标模型；
   - `available` → `POST /models/{id}/download`，轮询 `GET /models/{id}/download`
     直到 `downloaded`（有界超时，可被 systemd 停止打断）；
   - `error` → 记日志退出；
3. 若模型已在运行，先 `POST /models/unload`（加载是幂等的：进程已在运行时
   `load` 只会返回成功而不会重启它，因此活跃进程可能带着推理路径自动加载用的
   `embed.default_args`，必须停掉才能重新钉参数）；再 `POST /models/load`，
   `extra_args` 显式钉死
   `--pooling last --embd-normalize <标定值> --ctx-size 4096 --parallel 1`；
4. **不调用 `switch`**，保持 `_current_model` 指向 `bibit-embed`。

默认加载参数为 `--pooling last --embd-normalize 2 --ctx-size 4096 --parallel
1`；其中 `--embd-normalize 2` 与该 build 的内置默认值相同（已由
`llama-server --help` 确认），显式写出只是为了让"钉死"这件事在代码里可见。

为什么 `extra_args` 必须显式钉死：`merge_and_dedup_args` 中用户参数优先于
`default_args`，这是唯一能免疫 bibit 改写 `embed.default_args` 的手段。

已知局限：gateway 重启会丢失加载态，而推理路径的自动加载只用 `default_args`。
因此 provisioner 挂在 `After=spacemit-ai-gateway.service`，并在设置页 action 中可重复调用。
即使未重新 provision，向量语义仍与基线一致（已实测 1.0），只是 RSS 会回到默认值。

注意 memoryd 的 unit **不**与该 oneshot 做 `After=` 排序：首次 provision 可能要
下载 ~640 MiB，把 memoryd 排在它后面会让守护进程白等数分钟（期间 SQLite/FTS5
本可正常服务）。两者只在开机事务里并行启动，memoryd 靠后台重试补齐向量。

### 7.4 调用到 spacemit-ai-gateway 的接口清单

全项目只用到 `/v1/embed/*` 一个域，共 7 个接口；**运行期的只有第 1 个**，其余 6 个
只在打包安装/启动阶段由 provisioner 调一次。每个接口的成功判定与失败处理：

| # | 接口 | 调用方 | 用途 | 成功判定 | 失败处理 |
| --- | --- | --- | --- | --- | --- |
| 1 | `POST /v1/embed/embeddings` | memoryd（运行期，唯一） | 文本 → 1024 维向量：写入侧打底、检索侧查询 | HTTP 200 且 `data` 条数等于请求条数、`index` 不重复不越界、每个向量维度等于配置 `dims`、分量全部有限 | 非 200 → `Status(code)`；响应截断/字段缺失/数量不符/`index` 越界或重复/维度不符/非数字/非有限 → `Malformed`；连接或读写错误 → `Transport`；`WouldBlock`/`TimedOut` → `Timeout`；响应 > 16 MiB、批大小 0 或 > 128 → `Malformed`（在发请求前就拒绝）。**检索路径**把单请求超时钳在 2 s 预算内，失败即静默降级到词法检索；**写入/索引路径**失败由后台任务按 60 s 起的退避重试接管，不阻塞写入 |
| 2 | `GET /v1/embed/healthz` | provisioner | gateway 是否在 | HTTP 200 | 连不上 → 记一行日志 `return 0`（非致命；词法降级继续可用） |
| 3 | `GET /v1/embed/models` | provisioner | 列出模型，取目标 id 的状态 | HTTP 200 且 body 是数组 | 非 200 / 结构不符 / 找不到目标 id → 记日志 `return 0`（该 id 可能由后续 gateway 配置提供） |
| 4 | `POST /v1/embed/models/{id}/download` | provisioner | 触发下载（仅当状态不在 `downloaded`/`loading`/`loaded`） | HTTP 200 | 非 200 → `return 1` |
| 5 | `GET /v1/embed/models/{id}/download` | provisioner | 轮询下载进度 | HTTP 200 且 body 是对象、`status=downloaded` | 非 200 / 结构不符 / `status=error` → `return 1`；有界超时（默认 1800 s）后仍在下载 → 记一行日志 `return 1`（下载在 gateway 侧继续） |
| 6 | `POST /v1/embed/models/unload` | provisioner | 停掉已在运行的 memory 模型，好让下一次 load 决定它的启动参数 | HTTP 200 | 非 200 → `return 1` |
| 7 | `POST /v1/embed/models/load` | provisioner | 用钉死的 `extra_args` 加载并常驻 | HTTP 200（gateway 内部最多 health-check 120 s，脚本超时给 180 s） | 非 200 → `return 1` |

加载完成后 provisioner 再用接口 1 做一次金样比对：6 条中文文本的最低余弦 ≥ 0.999 才
算通过，否则 `return 1`——这是唯一一个"反正是后台任务却也按致命处理"的失败，因为它
意味着模型被换成了另一个向量空间（语义检索会静默变错，而不是变慢）。

**不调用的接口**：`POST /models/switch`（会抢走 bibit 的 `_current_model` 指针，见
§2）与 `deregister`（见 §9）。因此本方案对意图模块的可见影响只有"多了一个别的模型
在跑"，其注册行、`default_args`、当前模型、进程参数与 RSS 都不变。

### 7.5 文档与设置页

- 更新 `docs/CONFIGURATION.md`、`docs/MODEL_USAGE.md`、`README.md`、
  `docs/IMPLEMENTATION_STATUS.md` 中指向 18801 与自管 unit 的描述。
- 设置页（`bibit-memory-plugin/src/MemorySettingsPage.tsx`）当前只有 `enabled` 开关，
  后续可扩展为展示 gateway 侧模型状态与进度，并复用 provisioner 的同一路径。

### 7.6 索引影响

memoryd 的向量过期判定依据 `e.model!=?4 OR e.dims!=?5`
（`crates/memory-store/src/embedding.rs:1742`）。把 `model` 从 `qwen3-embedding-0.6b`
改为 gateway 的真实模型 id 后，**现有向量会被判 stale 并触发一次性后台重建**：

- 重建期间这些记忆退化为词法召回，用户无感；
- 重建是后台 best-effort，失败不影响检索主流程；
- 这是有意为之：让 label 与真实服务模型一一对应，未来 gateway 侧换模型能被自动发现。

若需要零重建上线（例如大库设备），可保持 `model` 不变并依赖"gateway 的
`embed.backend` 一改就必须同步 bump label"的运维约定；本方案不采用该折中。

**升级设备还多一步配置迁移**：`embedding` 段是显式配置，而 0.1.9~0.1.16 会在
设备第一次保存设置时把当时的默认值（`http://127.0.0.1:18801/v1` +
`qwen3-embedding-0.6b`）写进 `memory.config.json`。如果只改 `Default::default()`，
所有已升级设备都会继续指向已被退役的 18801 —— 升级"看起来成功"、语义检索却静默
退化成词法。因此 `read_config` 增加了 `converged_embedding`：**恰好等于这对旧
默认值**时收敛到网关取值（其余字段与任何自定义 endpoint 都不动），与既有的
`converged_backup_scopes` 同一套"读取时收敛、下次写入持久化"的做法。


### 7.7 安装期输出（对齐 bibit-intent）
bibit-intent 装完之后 dpkg 会讲三件事：模型初始化是异步的（在
`bibit-intent-models.service`）、它带的 MCP 由 Runtime 插件分发而不写用户主目录、
以及 `llm:` / `embed:` 两个模型各自的落盘路径。0.1.19 起 Memory 用同一套形状给出
对应的四行：
| 何时 | 输出 | 对应 bibit-intent |
| --- | --- | --- |
| 装 `bianbu-agent-memoryd` | `bianbu-agent-memoryd: model initialization runs asynchronously in bianbu-agent-memory-embedding-provision.service` | `bibit-intent: model initialization runs asynchronously in bibit-intent-models.service` |
| 装 `bianbu-agent-memoryd` | `bianbu-agent-memoryd: recall stays lexical until the Gateway has the embedding model` | 无对应句（见下） |
| 装 `bianbu-agent-memory`，且 Runtime 的 `bianbu.memory` 插件在位 | `bianbu-agent-memory: Runtime Memory Plugin ships this MCP; no per-user manifest is installed` | `bibit-intent: Runtime Bibit Plugin ships this MCP; no per-user manifest is installed` |
| provisioner 每次跑完（journal，不是 dpkg 输出） | `[memory-embedding-provision] verified Gateway model at /home/example/.cache/models/embed/Qwen3-Embedding-0.6B-Q8_0.gguf` | `embed: verified Gateway model at /home/example/.cache/models/embed/Qwen3-Embedding-0.6B-Q4_0.gguf` |
三处差异是有意的：
- **路径那行落在 journal**：bibit-intent 在 postinst 里同步跑模型迁移，所以能在安装
  阶段打印路径；Memory 的首次拉取是 ~640 MiB、按设计异步（§7.3），postinst 拿不到
  路径。把"刚验证过的是哪个文件"放进 provisioner 的 journal，语义相同
  （`systemctl status bianbu-agent-memory-embedding-provision` 直接可读），而且每次
  重跑——含 gateway 重启后 provisioner 重跑——都会重新打印。路径取自
  `GET /v1/embed/models` 的 `local_path`，在一致性自检之后重新读取：本次运行才下载完
  的模型，下载前那次读到的 `local_path` 还是 `null`。
- **多一句词法降级**：Memory 在 Gateway 没有模型时仍然可用，只是没有语义召回。这句
  把"装完就能用、语义要等一会儿"说在前面，省掉一次「为什么语义检索没结果」的误报。
- **MCP 那句带存在性判断**：`bianbu.memory` 插件由 `bianbu-agent-runtime` 提供，
  Memory 自己的两个包从不写用户主目录的 MCP 清单（两份 id 不同，会同时被加载）；
  插件不在时不打印，避免给出一个当时不成立的承诺。

## 8. 验证

**一致性闸门**

- `tests/data/embedding_parity_golden.json`（打包进 deb 的
  `/usr/share/bianbu-agent-memoryd/`）保存 6 条中文文本的基线向量，采集自现网
  18801 服务；provisioner 每次加载后逐条比对，最低余弦低于 0.999 即判失败并
  以退出码 1 结束 —— 这是兜住"bibit 改写 `default_args`"场景的自动闸门。
- 换模型时必须同步重采这份金样，否则闸门会（正确地）拦下加载。

**回归脚本（K3 现有资产）**

- `scripts/k3_vector_smoke.py`
- `scripts/k3_vector_perf_acceptance.py`
- `scripts/k3_semantic_e2e_runtime.py`
- `scripts/k3_embedding_quality_eval.py`
- `scripts/k3_production_install_verify.py`（打现网守护进程，验"装到设备上的包是否按新配置在跑"，见 §8.2）
- `scripts/k3_health_concurrency_probe.py`（回归闸门：负载中 `health` 不得再报 `degraded`，见 §8.2）

**指标门控**

- `MEM-NFR-02`：语义检索端到端 ≤ 2 s；
- gateway 停止时 memoryd 保持绿色并静默回退 FTS，日志不得出现
  `[semantic-embed] embedding failed` 风暴；
- gateway 恢复后语义检索无需重启 memoryd 即恢复。

**不影响意图模块的验证**

1. 改动前后 `GET /v1/embed/models` 中 `bibit-embed` 行逐字段一致；
2. `embed.backend`、`embed.default_args`、`port_pool` 未变；
3. `/v1/embed/healthz` 的 current model 仍为 `bibit-embed`；
4. 18800 进程的启动参数与 RSS 不变；
5. 以 `model=bibit-embed` 发一组固定文本，改动前后向量逐位一致；
6. 整机内存余量不低于改动前。

### 8.1 本轮结果（2026-09-22）

- `cargo test --workspace --locked`（密闭模式 `AGENT_MEMORY_EMBEDDING_DISABLED=1`）
  全绿：9 / 2 / 31 / 3 / 63 各套件 0 失败。`debian/rules` 的 `dh_auto_test`
  已改为带该变量，理由见该处注释。
- **反向证据**：模型尚未 provision 时，`store_flow::
  lexical_backfill_upgrades_legacy_namespaces_behind_the_fallback` 会失败 —— 它
  沿用默认 endpoint 且未打桩，属于既有的"依赖宿主环境"缺陷；模型 provision
  完成后同一用例直接通过，说明 memoryd 的 `EmbeddingClient` 真的经
  `http://127.0.0.1:18790/v1/embed` + `model=qwen3-embedding-0.6b-q8_0`
  取到了 1024 维向量并入库。
- `scripts/k3_vector_smoke.py`：`K3_VECTOR_SMOKE_PASS`。3 条改写查询全部
  rank 1 命中、FTS 对照全为 false、`forget` 清理派生向量、服务不可达时静默
  回退（`silent_fallback_no_error`）。检索耗时 80~181 ms。
- `scripts/k3_embedding_quality_eval.py`：`overall_pass=true`，
  `semantic_gain=0.30`（≥ 0.15 门槛），`prefix_delta=0.0`（前缀不劣化）。
- `scripts/k3_vector_perf_acceptance.py`（10 万条 / 8 并发 / 400 ops）：
  `overall_pass=true`。热相 `search_plus_read` P95 **111.8 ms**、冷相 P95
  **113.5 ms**（门槛 2000 ms），FTS 基线 P95 49.6 ms，四个相位 `error_count`
  全为 0，降级相位（杀掉 llama-server）照常通过。本次用 gateway 刚下载的
  `~/.cache/models/embed/Qwen3-Embedding-0.6B-Q8_0.gguf` 喂脚本自管的
  18901 实例（脚本刻意自持推理进程以控制被测参数），因此这部分验证的是
  memoryd 侧的向量/扫描/融合路径，与 gateway 的 proxy 叠加成本（实测 0.06 s
  热态）无关。
- `scripts/k3_semantic_e2e_runtime.py`：**本轮未跑** —— 它依赖设备上的
  Runtime 栈（`/home/example/personal-agent-runtime-memory-fix` + venv），该目录
  在当前设备上不存在。脚本本身已按 gateway 改造（`/v1/embed` 健康探测、
  `EmbeddingGuard` 走 `/models/unload` + `/models/load`），待 Runtime 栈就绪后
  补跑。
  **2026-09-23 已补跑**：Runtime 侧把 memory 插件做进了仓库与发行包，按 §8.6 起
  隔离栈后连跑两次全 PASS。
- `dpkg-buildpackage -b -uc -us`：验证打包链路，产出
  `bianbu-agent-memoryd_0.1.17_riscv64.deb` 与 `bianbu-agent-memory_0.1.17_all.deb`，
  安装清单与 0.1.17 版本戳正确。**注意 riscv64 上跑不到测试**：Ubuntu 的 dpkg
  vendor hook（`Dpkg::Vendor::Ubuntu::update-buildopts`）会对 riscv64 强制注入
  `DEB_BUILD_OPTIONS=nocheck`，dh 因此跳过 `dh_auto_test`。这正是 0.1.16 能在
  一个"依赖宿主 endpoint"的用例存在的情况下发出去的原因；`debian/rules` 现在
  带 `AGENT_MEMORY_EMBEDDING_DISABLED=1`，保证显式跑测试时是密闭的。
- 意图模块非回归（与本方案同机、改动前后对比）：`base.yaml` 逐字节一致；
  `bibit-embed` 与 `qwen3-embedding-0.6b-q4_0` 的注册行逐字段一致；
  `/v1/embed/healthz` 的 current model 仍为 `bibit-embed`；
  18800 进程 `lstart=15:41:59` 未变（从未重启），RSS 4.49 GB 基本持平；
  `model=bibit-embed` 请求持续返回 1024 维向量。

### 8.2 现网安装与切换验收（2026-09-22 19:31，K3 192.0.2.10）

本节把 0.1.17 真正装到设备上（此前只验证过打包链路），并直接在现网守护进程上
验收，而不是另起测试实例。验收脚本：`scripts/k3_production_install_verify.py`。

| 项 | 结果 |
| --- | --- |
| 打包 | `dpkg-buildpackage -b -uc -us` → `bianbu-agent-memoryd_0.1.17_riscv64.deb`（2 722 836 B）、`bianbu-agent-memory_0.1.17_all.deb`（84 742 B） |
| 安装 | `sudo dpkg -i` 退出码 0；两包均为 0.1.17；旧 `bianbu-agent-memory-embedding.service` 的文件与 enable 软链均消失（`is-enabled` = not-found），新增 `bianbu-agent-memory-embedding-provision.service` 软链 |
| 二进制一致性 | `/usr/libexec/bianbu-agent-memoryd` md5 `36d6cf3af692c477767480c338d0898d`，与设备上构出物逐字节一致；`strings` 同时含新常量 `http://127.0.0.1:18790/v1/embed`、`qwen3-embedding-0.6b-q8_0` 与仅用于迁移比对的旧值 `http://127.0.0.1:18801/v1`、`qwen3-embedding-0.6b` |
| 源码一致性 | 参与本次构建的关键文件在本地工作区与设备构建目录（`/home/example/mem-gateway-build`）md5 全等：`crates/memory-store/src/product.rs`（05a6ec28）、`debian/{rules,changelog,bianbu-agent-memoryd.postinst,bianbu-agent-memoryd.prerm}`、`packaging/bianbu-agent-memoryd.service`、`scripts/k3_vector_smoke.py`、`sdk/.../dplus.py`（b6fe7939）、`tests/data/embedding_parity_golden.json`（4bdcd9aa） |
| Python 侧 | SDK 包同样是最新源码：`/usr/lib/python3/dist-packages/bianbu_agent_sdk/memory/{dplus,client,long_term}.py` 的 md5 与本地一致；adapter 服务 19:31:53 随装包重启 |
| 进程 | memoryd 重启（MainPID 1898308，19:31:45），`--version` 报 0.1.17；`bianbu-agent-memory-embedding` inactive、`...-embedding-provision` active(exited 0)、adapter active |
| 端口 | 旧自管 18801 已无监听（连接被拒）；18790 网关、18800（`bibit-embed` Q4_0）、18804（memory Q8_0，钉死参数）在跑 |
| 生效配置 | 对现网守护进程 `privacy.settings.get` 返回 `base_url=http://127.0.0.1:18790/v1/embed`、`model=qwen3-embedding-0.6b-q8_0`、`dims=1024`。磁盘上 `memory.config.json` 仍是 0.1.16 写下的 18801 旧值（mtime 未变），说明收敛发生在读取时、下次写设置才落盘——迁移逻辑确实生效，否则会静默降级成纯词法 |
| provisioner | 首启日志 `loaded with --pooling last --embd-normalize 2 --ctx-size 4096 --parallel 1`、`parity: worst cosine 1.000000 over 6 texts`；`GET /v1/embed/models` 中 `qwen3-embedding-0.6b-q8_0` status=loaded |
| 存量向量重建 | 重启后 60 s 内后台 embedding 任务按命名空间整批重建（如 `com.bianbu.runtime` `indexed=6`），全部 `state=success`、`failed=0`；生产 Runtime 空间为 `long_term_memories=6 / memory_embeddings=6` |
| 现网端到端 | `K3_PRODUCTION_VERIFY_PASS`，四条闸门全 true：生效端点=网关；3 条探针记忆各写入 1 条向量；三条与存储文本 trigram 零重叠的改写查询全部 rank 1 命中（83~168 ms）；用守护进程自己的 adapter 确认令牌 forget 后派生向量清零，探针空间回到 0/0 |
| 意图模块 | 本轮未重启 18800（`bibit-embed`），`/v1/embed/healthz` 仍 `{"status":"ready","model":"bibit-embed"}`；`configs/base.yaml` 未改动 |

**health 的 `degraded` 是并发写下的误报（0.1.17 已复现定性，0.1.18 已修）**

0.1.17 的 `system.health` 在池化连接上直接跑 `PRAGMA quick_check`：只要该库的
write-ahead log 里还留着未 checkpoint 的帧，SQLCipher 就返回
`malformed inverted index for FTS5 table main.long_term_fts[2]` 并把 status 打成
`degraded`；守护进程静下来后同一命令返回 `ok`。复现脚本：
`scripts/k3_health_concurrency_probe.py`（2 个写者 + 背靠背两次取样的读者，全程只动
探针命名空间）。

| 信号 | 实测（2026-09-22，K3） |
| --- | --- |
| 负载下取样 | 11 次取样中 10 次 degraded，判词在 `long_term_fts` 与 `long_term_fts2` 之间来回漂移 |
| 相邻两次调用 | 出现 2 次"同一连接连续两次调用判词不同"（另有 5 次 flap 见 load 版脚本） |
| 纯读负载 | 65 s 内 24 次连续取样全部 `ok`（读者不产生 WAL 帧） |
| 功能不变量 | 负载期间 0 次丢失应召回的标记记录，0 次召回已 forget 的记录（陈旧索引项才会让它复活） |

判为**误报**而非数据损坏的依据：判词在相邻两次调用之间翻转、在两张表之间漂移，
静默后自行恢复（真实损坏不会自愈），且检索结果始终正确。已排除的原因：
`mmap_size=0`（无 mmap 竞态）；daemon 的写全部串行在同一把 `write_lock` 下的单个
`IMMEDIATE` 事务里，落盘状态按构造自洽；stock SQLite 3.43.1 在 98 MB WAL 库上用
同样写模式 + 并发写者跑 `quick_check` 复现不出（0/101）；`INSERT OR REPLACE`、
删除后复用 rowid 单独测也干净。精确触发点在 SQLCipher + 多连接这一层。

**修复过程中把触发条件量准了一层（2026-09-22 21:00–21:30，同机复测）**：先按
"检查窗口内不能有提交"做（写者互斥 + 清空 WAL + 安静窗口），闸门仍然失败，于是用
`AGENT_MEMORY_HEALTH_CHECK_INTERVAL_MS=0` 让每次 `system.health` 都做一次独立检查，
在写入高峰后每 5 s 取样一次，量到的是：

| 时刻（停写后） | 判词 |
| --- | --- |
| 0–6 s | `busy`/`recent-activity`（静默窗口未满） |
| 6.9 s – 104 s | `degraded`：**每一次**取样都复现，连续 15 次，`attempts=1`（每次都真的取到了样） |
| 110 s – 248 s | `ok`：每一次取样都 `ok`，连续 24 次 |

即：这条判词与"检查窗口内有没有提交"无关——数据库已经安静 100 s，它照样复现；然后
又自己消失，之后不再出现（后续 140 s 内 24 次全 `ok`）。同一窗口内功能不变量不破、
检索正常。所以"与写者互斥 + 清空 WAL"并不足以让读数可信。

**第一版修法（按时间确认）被自己的闸门否掉**：先做的是"投诉要连续复现
`AGENT_MEMORY_HEALTH_CONFIRM_MS`（默认 5 min）才发布为 `degraded`"。单次写入高峰后
跑闸门通过（后 91 s 里 `state=unconfirmed`、`status=ok`、判词 0 次进入 `status`），
**但紧接着第二次跑（3 写者 + 2 读者、负载 60 s）失败**：投诉从上一轮起一直没消失，
两轮负载叠加使它"连续复现"超过 5 min，于是被发布成 `degraded`（`after_load_degraded: 1`，
`EXIT=1`）。也就是说，这条判词的存活时间由**写入活动**决定，不由墙钟决定——只要设备
在持续写（真实用户场景），按时间确认迟早会把它发布出来，等于没修。这条修法已废弃。

**0.1.18 的处置（已实施）**：`health` 只在安静、WAL 为空、与写者互斥时取样；**那条
判词不再算判词**，测不到就说测不到：

| 机制 | 作用 |
| --- | --- |
| 检查期间持有 `write_lock` | daemon 自己的客户端写事务与检查互斥，检查窗口内不可能有客户端提交落地 |
| `wal_checkpoint(TRUNCATE)` + `BEGIN IMMEDIATE` + `busy_timeout=0` | 先把 WAL 折回主库并要求"已清空"，再用 `BEGIN IMMEDIATE` 冻结该状态，事务内确认 `-wal` 仍是 0 字节；后台任务（按设计不取 `write_lock`）正持有写锁、或在这两步之间提交时，立即 `SQLITE_BUSY`——可靠的"本次取样无结论"信号 |
| 静默窗口 `AGENT_MEMORY_HEALTH_SETTLE_MS`（默认 3 s） | 距最近一次写入（含后台批处理，见 `ActivityClock`）不足这个时间就不取样，报 `busy`/`recent-activity`（能挡住上表前 6 s 那一段） |
| FTS5 inverted-index 那条判词不参与 `status` | 它是这个 build 对**完好**索引给出的文本：写入高峰后必现、漂移于两张 FTS 表之间、自行消失、功能不变量与检索全程正常，而且"复现多久算真"没有可靠的界（见上一段）。因此它只以 `database_check.complaint` 原文呈现（`state=complaint`、`reason=fts5-inverted-index`），`verdict` 仍为 `ok`；**其他任何**缺陷文本首次安静取样即发布为 `degraded`（含"wrong number of entries in index …"这类同样提到 FTS5 但语义不同的文本） |
| 结论缓存 `AGENT_MEMORY_HEALTH_CHECK_INTERVAL_MS`（默认 30 s） | 整库检查在 K3 现网库上约 406 ms；缓存后轮询不再每次付这个代价，相邻两次调用也不可能给出不同判词；在案的投诉按同一间隔重取样（消失即清除） |
| 有界重试 3 次 × 25 ms | 短暂的后台事务结束后仍能拿到结论，不把"写者恰好路过"升级成"无结论" |

响应形状（新增字段，旧字段语义不变）：

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `status` | `ok` / `degraded` / `unknown` | 只由"已发布的判词"推导；投诉待确认、写者占库、从未取样都不改变它 |
| `database` | `ok` / 具体缺陷描述 / `busy` / `unknown` | 判词本体；`busy` = 本次没测到，`unknown` = 还没有取过样，两者都不因此报 `degraded` |
| `database_check.state` | `checked` / `cached` / `busy` / `complaint` | 本次响应的判词是怎么来的：刚取样 / 复用缓存 / 没测到 / 有在案的投诉 |
| `database_check.reason` | `null` / `recent-activity` / `database-busy` / `fts5-inverted-index` | 为什么是现在这个 `state` |
| `database_check.verdict` | `ok` / 缺陷描述 / `null` | 已发布的判词 |
| `database_check.complaint` | `null` / 缺陷描述 | 不当作判词的投诉原文（目前只有 FTS5 inverted-index 那一类） |
| `database_check.checked_at_ms` · `age_ms` · `interval_ms` · `attempts` | 数值 | 判词时间戳、其年龄、缓存窗口、本次尝试次数 |

`degraded` 从此只意味着"`quick_check` 报了不是 FTS5 inverted-index 的缺陷"。
回归闸门：`scripts/k3_health_concurrency_probe.py` 在负载中要求 0 次 `degraded`、
0 次相邻取样翻转、功能不变量不破，写者停下后继续取样（`AFTER_LOAD_SAMPLES` ×
`AFTER_LOAD_INTERVAL`，默认覆盖停写后约 3 min，长于实测的 ~100 s）并要求这一段也
0 次 `degraded`、0 次翻转，通过时打印 `K3_HEALTH_CONCURRENCY_PROBE_OK`（不通过退出
码 1）；Rust 侧新增单元测试覆盖判词分类与响应形状（两种表名都识别、语义不同但同样
提到 FTS5 的文本仍算缺陷、在案投诉不参与 `status`、`unknown` 与 `busy` 的区分）与
集成用例
`health_reports_busy_instead_of_degraded_while_a_writer_holds_the_database`、
`health_reuses_the_integrity_verdict_within_the_check_interval`、
`health_reports_an_fts5_complaint_without_calling_the_database_degraded`（最后一个同时
断言召回不受影响）。

告警/监控消费方注意：`status` 现在可能是 `unknown`（此前只有 `ok`/`degraded`）。
若规则写成 `status != "ok"`，请改为只对 `degraded` 触发；`unknown` 的含义是
"当前有写在并发、本次没测到"或"还没取过样"。想观察那条瞬时判词而不告警，看
`database_check.state == "complaint"` 与 `database_check.complaint`。

### 8.3 0.1.18 现网复测（2026-09-22 22:10–22:52，K3 192.0.2.10）

按“连看两次”跑：同一版二进制、同一台设备、同一批闸门连续跑两轮。共跑了两对：

- **A 对**（22:10–22:35，22:27 那次安装）：先 2 写者 + 2 读者 75 s，紧接着 3 写者 + 2 读者 60 s。
- **B 对**（22:36–22:52，源码树重新同步后重打包重装）：同样先 2 写者 + 2 读者 75 s，
  紧接着 3 写者 + 2 读者 60 s。

两次打包的产物**逐字节相同**（`bianbu-agent-memoryd_0.1.18_riscv64.deb` 里
`usr/libexec/bianbu-agent-memoryd` 的 md5 均为 `6ea37ae09dc3a9d14a7da22af205f35d`），
两包之间只差 `.md` 文档；因此 A、B 两对合起来是同一版二进制上的四次独立“连看”，
结论一致。

| 项 | 结果 |
| --- | --- |
| 打包 | `dpkg-buildpackage -b -uc -us`（root + `CARGO_HOME=/home/example/.cargo`）各一次 → A：`bianbu-agent-memoryd_0.1.18_riscv64.deb`（2 742 682 B）、`bianbu-agent-memory_0.1.18_all.deb`（87 248 B）；B：2 744 780 B / 87 248 B（差额只来自文档改动） |
| 安装 | 两次 `sudo dpkg -i` 退出码均 0；两包均 0.1.18 |
| 二进制一致性 | `/usr/libexec/bianbu-agent-memoryd` md5 `6ea37ae09dc3a9d14a7da22af205f35d`，与设备构建目录 `target/release/bianbu-agent-memoryd` 经 `strip --remove-section=.comment --remove-section=.note --strip-unneeded`（= `dh_strip` 的变换）后的 md5 逐字节一致；A、B 两次打包出来的该 md5 也相同 |
| 源码一致性 | 本地工作区与设备构建目录 md5 全等：`crates/memory-store/src/lib.rs`（390a7f9c）、`background.rs`（b33effd1）、`tests/store_flow.rs`（8adb79cb）、`debian/changelog`（4f392797）、`docs/CONFIGURATION.md`（af260673）、`scripts/k3_health_concurrency_probe.py`（2a59578e）、`scripts/k3_production_install_verify.py`（9c39260b） |
| 进程 | memoryd `--version` 报 0.1.18；A 对 MainPID 2033876、22:27:09 启动，B 对 MainPID 2040694、22:36:23 启动；两次都**无 drop-in、无环境覆盖**（`DropInPaths` 只剩 `permissions.conf`，`Environment=` 为空） |
| Rust 测试 | `AGENT_MEMORY_EMBEDDING_DISABLED=1 cargo test --workspace --locked`：9 / 2 / 36 / 3 / 66 各套件 0 失败 |
| 闸门 1（2 写者 + 2 读者，负载 75 s） | A、B 两对均 `K3_HEALTH_CONCURRENCY_PROBE_OK`。A：负载内 207 次取样 0 次 `degraded`、0 次相邻翻转，停写后 16 次取样（约 93 s）同样 0/0，判词仍在但以 `state=complaint`、`reason=fts5-inverted-index`、`status=ok`、`database=ok` 呈现。B：负载内 226 次取样 0/0，停写后 16 次 0/0，判词自行消失（`state=cached`、`complaint=null`） |
| 闸门 2（3 写者 + 2 读者，负载 60 s，紧接着跑） | A、B 两对均 `K3_HEALTH_CONCURRENCY_PROBE_OK`。A：负载内 152 次取样 0 次 `degraded`、0 次翻转，停写后 16 次 0 次 `degraded`，末尾回到 `state=cached`、`complaint=null`；**第一版按时间确认的修法就是死于这一轮**（见上一节）。B：负载内 159 次取样 0/0，停写后 16 次 0/0，末尾停在 `state=complaint`、`reason=fts5-inverted-index`、`verdict=ok`、`status=ok`——判词在案而 `status` 不受影响，正是本轮要证明的形状 |
| 功能不变量 | 四轮均为 0 次丢失应召回的标记记录、0 次召回已 forget 的记录；探针空间回收后 0/0 |
| 闸门 3（现网安装） | `scripts/k3_production_install_verify.py` → `K3_PRODUCTION_VERIFY_PASS`，5 闸门全 true（生效端点为网关、每条探针记忆各产生 1 条向量、三条改写查询全部 rank 1 命中、health 无幻影 `degraded`、forget 清空派生向量）；B 对安装后复跑同样通过 |
| 闸门 4（需求验收） | `scripts/k3_requirements_acceptance.py` → `K3_REQUIREMENTS_ACCEPTANCE_OK`（退出码 0，无断言失败）；B 对安装后复跑同样退 0 |
| 意图模块 | 18800 的 `llama-server` 全程仍是 20:03:15 那一个进程（PID 1929219、`Qwen3-Embedding-0.6B-Q4_0`、`--pooling mean`），A、B 两对都没重启过它；`/v1/embed/healthz` 的 current model 仍是 `bibit-embed`；`/opt/spacemit-ai-gateway/configs/base.yaml` md5 `c8d5582e`、mtime 20:03:08，未被本方案写入 |
| memory 侧模型 | 18802 的 `qwen3-embedding-0.6b-q8_0`（`--pooling last --embd-normalize 2 --ctx-size 4096 --parallel 1`）由 provisioner 随 memoryd 拉起：A 对 22:27:10、B 对 22:36:23 |

**最后落盘（0.1.18）**：§8.3 定稿后又重打包重装了一次，只为把本文档送进
`/usr/share/doc/bianbu-agent-memoryd/GATEWAY_EMBEDDING_INTEGRATION.md.gz`（文档随
`bianbu-agent-memoryd` 包分发）。该次打包出来的二进制与 A、B 两对所用者逐字节相同
（md5 `6ea37ae09dc3a9d14a7da22af205f35d`），所以 A、B 两对的结论对最终设备状态同样成立；
装完后重启了 memoryd，`--version` 仍为 0.1.18、`DropInPaths` 与 `Environment=` 不变。0.1.19 的安装期输出与闸门见 §8.4。


### 8.4 0.1.19 安装期输出（2026-09-22 23:05–23:32，K3 192.0.2.10）
0.1.19 没有改 Rust 代码，只加了 §7.7 的安装期提示与 provisioner 的那行路径。版本号
由 `debian/changelog` 经 `build.rs` 打进二进制，所以二进制 md5 相对 0.1.18 仍有变化
（`6ea37ae0…` → `e85afc58…`）。
| 项 | 结果 |
| --- | --- |
| 打包 | `bianbu-agent-memoryd_0.1.19_riscv64.deb`（2 746 254 B）、`bianbu-agent-memory_0.1.19_all.deb`（87 946 B） |
| 安装 | `sudo dpkg -i` 退出码 0，0.1.18 → 0.1.19 |
| 安装输出 | `正在设置 bianbu-agent-memoryd (0.1.19) ...` 之后两行：`bianbu-agent-memoryd: model initialization runs asynchronously in bianbu-agent-memory-embedding-provision.service`、`bianbu-agent-memoryd: recall stays lexical until the Gateway has the embedding model`；`正在设置 bianbu-agent-memory (0.1.19) ...` 之后一行：`bianbu-agent-memory: Runtime Memory Plugin ships this MCP; no per-user manifest is installed` |
| provisioner 输出 | journal 多一行 `[memory-embedding-provision] verified Gateway model at /home/example/.cache/models/embed/Qwen3-Embedding-0.6B-Q8_0.gguf`，位于 `parity: worst cosine 1.000000 over 6 texts` 之后 |
| 二进制一致性 | `/usr/libexec/bianbu-agent-memoryd` md5 `e85afc58e2a050158627ec75ae758bf1`，与设备构建目录 `target/release/bianbu-agent-memoryd` 经 `strip --remove-section=.comment --remove-section=.note --strip-unneeded` 后逐字节一致 |
| 进程 | memoryd `--version` 报 0.1.19（与 `dpkg -s` 的 `Version: 0.1.19` 一致）；MainPID 2067539、23:15:20 启动；**无 drop-in、无环境覆盖** |
| 闸门 5（连看两次） | 0.1.19 上再跑一对：2 写者 + 2 读者 75 s，紧接着 3 写者 + 2 读者 60 s，两轮均 `K3_HEALTH_CONCURRENCY_PROBE_OK`：负载内 190 / 163 次取样 0 次 `degraded`、0 次相邻翻转，停写后各 16 次 0/0；第二轮末尾仍是 `state=complaint`、`reason=fts5-inverted-index`、`verdict=ok`、`status=ok`——与 §8.3 同形 |
| 闸门 3 / 4 | `scripts/k3_production_install_verify.py` → `K3_PRODUCTION_VERIFY_PASS`（5 闸门全 true）；`scripts/k3_requirements_acceptance.py` → 退出码 0 |
| 意图模块 | 18800 仍是 20:03:15 那个进程（PID 1929219、Q4_0、`--pooling mean`），本轮未重启；`/v1/embed/healthz` 仍报 `bibit-embed` |

本节只覆盖安装期输出；语义检索按 `MEM-NFR-02` 与 §7 的"功能 / 性能 / 质量 / 供给"
四条链路的验收矩阵见 §8.5。

### 8.5 语义检索验收矩阵（0.1.19，2026-09-22 23:16 – 2026-09-23 00:10，K3 192.0.2.10）

四道门跑在同一版已安装二进制上（§8.4 的 `e85afc58e2a050158627ec75ae758bf1`），
脚本一律以 `--memoryd /usr/libexec/bianbu-agent-memoryd` 指向 `/usr/libexec` 下那个
**装出来的**守护进程，而不是构建目录里的副本。规格依据：`MEM-NFR-02`（语义检索
P95 ≤ 2 s，不可用时降级回 FTS）与 `MEM-INT-05-01`。

| 门 | 脚本 | 结果 |
| --- | --- | --- |
| VEC-04 功能冒烟 | `scripts/k3_vector_smoke.py` | `K3_VECTOR_SMOKE_PASS`，6 项子闸门全 true：写入即内联打底向量、3 条改写查询全部 rank 1 命中、FTS 反例不产生假命中、forget 清空派生向量、已 forget 的不再被召回、gateway 缺席时静默降级且不报错（`/tmp/vec_smoke_019.json`，23:46） |
| VEC-07 性能（缓存开） | `scripts/k3_vector_perf_acceptance.py --records 100000 --clients 8 --ops 400` | `overall_pass: true`；热 `search_plus_read` P95 57.094 ms、冷 P95 55.468 ms（闸门 2000 ms），0 错误，降级相位 400 次查询 0 错误；建 10 万条 973.6 s（≈103 条/s），写入 ack P95 10.5 ms，查询侧 embed P95 359 ms（`/tmp/vec_perf_019.json`，23:43） |
| VEC-07 性能（缓存关，严格） | 同上，加 `AGENT_MEMORY_SEARCH_CACHE=0`、`--ops 60` | `overall_pass: true`；10 万条、8 客户端：热 `search_plus_read` P95 **554.4 ms**、冷 P95 **546.1 ms**（闸门 2000 ms），P99 4512 / 4331 ms、max 4738 / 4697 ms，0 错误；查询侧 embed P50 344.5 / P95 366.1 / P99 379.0 ms；写入 ack P95 10.679 ms；建 10 万条 983.1 s；对照的纯 FTS 基线 P95 61.8 ms、健康值时降级路径 P95 62.3 ms（`/tmp/vec_perf_nocache.json`，00:08） |
| VEC-08 质量 | `scripts/k3_embedding_quality_eval.py` | `overall_pass: true`；语料 300、金标 100，语义增益 0.30（闸门 0.15），前缀扰动 Δ 0.0（`/tmp/vec_quality_019.json`，23:47） |
| VEC-10 供给链路 | `packaging/bianbu-agent-memory-embedding-provision` + 直接打 gateway 接口 | 四条边界都按 §7.4 的约定走：gateway 缺席 → 记日志退 0；`GET /v1/embed/models` 里没有目标 id → 退 0；`--check` 只读 → 退 0；`POST /v1/embed/models/unload`（0.34 s）后意图的 18800/18801 不受影响，下一次具名请求自动加载 4.27 s（新 PID 落 18802），`systemctl restart bianbu-agent-memory-embedding-provision.service` 重钉参数后 parity 1.000000 并打印 `verified Gateway model at …/embed/Qwen3-Embedding-0.6B-Q8_0.gguf` |
| 闸门 3 / 4 / 5 | `scripts/k3_production_install_verify.py`、`scripts/k3_requirements_acceptance.py`、`scripts/k3_health_concurrency_probe.py` | 见 §8.4：`K3_PRODUCTION_VERIFY_PASS`（5 闸门全 true）、需求验收退 0、连看两次 190 / 163 次取样 0 次 `degraded` |
| 意图模块非回归 | 18800 的 `llama-server`、`/v1/embed/healthz` | 全程仍是 20:03:15 那一个进程（PID 1929219、`Qwen3-Embedding-0.6B-Q4_0`、`--pooling mean`），healthz 的 current model 仍是 `bibit-embed` —— 本方案没碰过意图模块 |

**两处必须说清楚的口径**（不写清楚，读表的人会得到比事实更好的结论）：

- 缓存开那一行的 P95（57 ms）**不是** embedding 的真实延迟：memoryd 的
  `search_cache`（`AGENT_MEMORY_SEARCH_CACHE`，无 TTL）命中后整条查询不再打
  gateway，所以那一轮甚至比纯 FTS 相位还快。要报 `MEM-NFR-02` 就用缓存关那一行：
  每条查询都真的过一次 `POST /v1/embed/embeddings`（查询侧 P95 366 ms），端到端
  `search_plus_read` P95 554 ms，仍在 2 s 预算内；降级路径 P95 62 ms，远在预算内。
- P95 达标不等于尾延迟达标：8 客户端并发时 P99 约 4.5 s。原因是 18901 上那台
  llama-server 按 `--parallel 1` 单槽串行，8 路并发在单槽上排队。单用户交互
  （§8.1 的 1.5 s 预算）与 P95 闸门都满足；若要把 P99 也压进 2 s，得调
  `--parallel`（换来 RSS 上升）或给检索路径设更短的超时后降级，本方案按现状
  交付并在此备案。

**测法口径**：VEC-07 跑的是隔离的临时守护进程与临时库（不碰现网状态），种子阶段
用即时回答的假 embedding 服务把 10 万条灌进去（真模型灌要 ≈1 小时，而扫描成本与
向量内容无关），随后把 `base_url` 热切到与 gateway 同款同参的本地 llama-server
（`Qwen3-Embedding-0.6B-Q8_0`、`--pooling last --ctx-size 4096 --parallel 1`）跑热/冷
两相。也就是说这两行测的是"模型 + 调度器 + 10 万行扫描"的成本，不含 gateway 那一跳；
那一跳已单独实测（§5：gateway `POST /v1/embed/embeddings` 热态 0.06 s，直连 0.03 s），
且 VEC-04 与闸门 3 都在真实 gateway 路径上跑通，故结论可迁移。

**最后落盘（0.1.19）**：§8.5 定稿后再打一次包重装，把本文档（含 §8.5）与
`tests/data/embedding_parity_golden.json` 的 `source` 描述一并送进包内 —— 原描述写的
`127.0.0.1:18801` 已随自托管推理栈退役而不再是有效口径，金样向量本身一个数字都没改。
这次 `debian/changelog` 未变，因此 `build.rs` 打出的版本相同、二进制与上面那一版
逐字节相同，provisioner 的 parity 自检仍报 1.000000；装完后 memoryd 重启，`--version`
仍为 0.1.19。

**落盘后核验（2026-09-23 00:12，K3）**：装完这次包后逐项回读，全部对上：`/usr/libexec/bianbu-agent-memoryd`
的 md5 仍是 `e85afc58…`（这次只动文档与金样的 `source`，`debian/changelog` 未变、
`build.rs` 打出的版本号不变，所以二进制与 §8.4 逐字节相同）；包内
`/usr/share/doc/bianbu-agent-memoryd/GATEWAY_EMBEDDING_INTEGRATION.md.gz` 解压后与仓库里的
`docs/GATEWAY_EMBEDDING_INTEGRATION.md` 逐字节相同（含本节），包内
`/usr/share/bianbu-agent-memoryd/embedding_parity_golden.json` 与仓库中的 md5 同为
`230d922e…`；`--version` 与 `dpkg -s` 都是 0.1.19；`DropInPaths` 只剩 `permissions.conf`、
`Environment=` 为空；provisioner 随安装重跑，`unload → load(钉死参数) → 金样` 之后
报 `parity: worst cosine 1.000000 over 6 texts` 与 `verified Gateway model at
…/embed/Qwen3-Embedding-0.6B-Q8_0.gguf`；装完复跑 VEC-04 冒烟得 `K3_VECTOR_SMOKE_PASS`、
闸门 3 得 `K3_PRODUCTION_VERIFY_PASS`（5 闸门全 true，含 `effective_endpoint_is_gateway`
与 `health_reports_no_phantom_degradation`）、闸门 4 退 0。这两次复跑跑在这一次安装上，
其二进制与金样与最终落盘的包逐字节相同，只有 `docs/` 一行的差别，故结论同样成立。
意图模块的 18800 全程仍是 PID 1929219（20:03:15）那一个进程，`/v1/embed/healthz`
仍报 `bibit-embed`。

### 8.6 VEC-09 Runtime 端到端补跑（2026-09-23，K3 192.0.2.10）

§8.1 把 VEC-09 记为"本轮未跑"，缺的是设备上的 Runtime 栈。2026-09-23 复查：缺的那半块
在 Runtime 侧补上了，现在可以跑通。

**Runtime 侧现状（本次核对）**

- 源码：`https://github.com/spacemit-com/bianbu-agent-memory.git` 的 `main` 已到 `0528eb9d`
  （2026-09-22），仓库里带 `resources/plugins/bianbu.memory/`（plugin.yaml、config.yaml、
  `hooks/memory.before_run`、`hooks/memory.after_run`、`mcps/memory/mcp.json`、
  `skills/memory-guide`）与 `docs/memory-plugin.md`，另有 K3 用的
  `scripts/k3-agent-memory-runtime-start.sh`、`scripts/k3-agent-memory-stack-start.sh`
  （本次 `git clone` 取到设备 `/home/example/rt-main` 供核对）；
- 设备上装的 `bianbu-agent-runtime 0.1.120` 已经带同一份插件
  （`/usr/share/bianbu-agent-runtime/plugins/bianbu.memory`），生产 Runtime（9899）在跑
  且加载了它，注册表 `/run/bianbu/agent-runtime/memory-registry` 有活跃注册；
- 因此**不再需要** `/home/example/personal-agent-runtime-memory-fix` 这类源码 checkout
  + venv（那个目录此前已被清理，现在也不必要）：装好的 venv 加一份开发配置就够。

**跑法**

新增 `scripts/k3_semantic_e2e_stack_start.sh`：起隔离开发栈 —— 9898 的 Runtime（用装好的
venv，`BIANBU_MEMORY_ENGINE=native`）+ 一台 `--development-identity` 的 memoryd（独立
socket / 库 / key package，`--development-agent-name` 与 Runtime 侧的测试专家 id 对齐），
状态在 `/home/example/e2e-vec09`，与现网 9899 那一对互不影响。

为什么不能直接用现网那一对：现网 memoryd 按 Runtime 注册表判权，裸 SDK 客户端会被拒 ——
实测 `Unauthenticated: runtime registration was not found`，而 VEC-09 的 A/B 断言要的
正是这条直连通道。改成 `--development-identity` 后守护进程按 `SO_PEERCRED` 信任同 uid
客户端，问题即解（`BIANBU_MEMORY_DEV_CONFIRM_TOKEN` 只管删除类破坏性动作，本用例不碰）。

**结果（连跑两次：先手工栈、再用上面的脚本从空目录重建，结论一致）**

| 项 | 结果 |
| --- | --- |
| 命令 | `python3 scripts/k3_semantic_e2e_runtime.py --base-url http://127.0.0.1:9898 --socket /home/example/e2e-vec09/memoryd.sock --registry /home/example/e2e-vec09/memory-registry --agent-id com.bianbu.ee2b4775b346` |
| 写入 | 会话 A 让 Runtime 的测试专家（测试专家 / `com.bianbu.ee2b4775b346`）通过自然对话记住「终端和编辑器配色偏好是远山黛」，落库后词法查询命中 rank 1 |
| 反例（关 embedding） | 改写查询 `界面颜色主题喜欢哪种风格` 在 FTS 侧 rank 0 —— 证明下面的命中确实是语义的，不是词法 |
| 正例（开 embedding） | 同一条改写查询 rank 1（`semantic_pass`），走 memoryd 的 `search_headers` 向量分支 |
| Runtime 侧回答 | 会话 B 的回答含标记（`runtime_soft_pass`），且点明记录范围是"终端和编辑器"、没有过度外推 |
| 降级 | 用 gateway `POST /v1/embed/models/unload` 停掉向量后端，会话 C 仍能回答（FTS 回退），随后 `POST /v1/embed/models/load` 用钉死参数恢复（`restarted: true`） |
| 总判定 | `overall_pass: true`（两次一致），状态行 `K3_SEMANTIC_E2E_RUNTIME` |
| 现网非回归 | 意图模块 18800 仍是 20:03:15 那个进程、`/v1/embed/healthz` 仍报 `bibit-embed`；生产 memoryd 与 Runtime（9899）全程 active；`qwen3-embedding-0.6b-q8_0` 在 guard 重启后仍是 `loaded` |

开发栈留在设备上跑着（`/home/example/e2e-vec09`，脚本可重复执行以重建）；要停掉：
`kill "$(cat /home/example/e2e-vec09/runtime.pid)" "$(cat /home/example/e2e-vec09/memoryd.pid)"`。

## 9. 风险与回滚

| 风险 | 处置 |
| --- | --- |
| bibit 的 `configure-gateway-embedding.py` 改写 `embed.default_args` | provisioner 用 `extra_args` 钉死关键参数；一致性自检兜底 |
| gateway 重启丢失加载态 | 推理路径会自动重新加载（实测 6.3 s 冷启动）但用的是共享 `embed.default_args`，RSS 会从 1.78 GB 涨到 4.98 GB；provisioner 随 gateway 启动重跑、先 unload 再 load，把参数钉回去；向量语义不受影响 |
| 首次请求触发自动加载拖慢检索 | 实测冷启动 6.3 s，远超 1.5 s 交互预算，因此必须由 provisioner 在启动阶段预加载 |
| provisioner 与 gateway 启动竞争 | unit 用 `After=` + 有界重试；失败不影响 memoryd |
| gateway 成为语义检索单点 | memoryd 保持词法降级路径；gateway 停止不阻塞写入与 FTS |
| `deregister` 泄漏 llama-server 进程 | 上报 gateway；本方案不使用 deregister |

**回滚**：把 `EmbeddingConfig` 的 `base_url` / `model` 改回 18801 与
`qwen3-embedding-0.6b`，恢复 `packaging/bianbu-agent-memory-embedding.service`
与下载脚本，重新启用 unit。模型副本建议保留一个发布周期。

## 10. 待办

- [x] `--embd-normalize` 一次性标定（实测该 build 默认即 2，取 2 与基线余弦
      1.000000；敏感项是量化与 `--pooling`，不是 normalize）
- [x] `EmbeddingConfig::default()` 更新
- [x] 退役自管推理栈（unit / 下载脚本 / rules / postinst / prerm）
- [x] provisioner 脚本与 unit
- [x] D+ 引擎 embedder 默认值同步指向 gateway
- [x] 文档更新
- [x] K3 验证 —— 8.1 已跑通 provisioner 端到端、smoke、quality、10 万条 perf、
      意图模块非回归与打包
- [x] `system.health` 不再把 FTS5 瞬时判词当数据损坏发布为 `degraded`
      （0.1.18：独占取样 + 静默窗口 + 结论缓存，判词改走 `database_check.complaint`；
      根因、被否掉的第一版修法、闸门与两对“连看”证据见 §8.2、§8.3）
- [x] 安装期提示对齐 bibit-intent（0.1.19：模型初始化异步、词法降级、MCP 归属
      三行 + provisioner 打印已验证模型路径；见 §7.7、§8.4）
- [x] `scripts/k3_semantic_e2e_runtime.py`（2026-09-23 补跑：Runtime 0.1.120 自带
      bianbu.memory 插件，用新增的 `scripts/k3_semantic_e2e_stack_start.sh` 起隔离栈，
      连跑两次全 PASS；见 §8.6。原先缺的 `/home/example/personal-agent-runtime-memory-fix`
      不再需要）
- [x] 把这版 deb 安装到设备完成切换（2026-09-22 19:31）：两包升到 0.1.17，
      旧 `bianbu-agent-memory-embedding.service` 随 `prerm` 停止并移除软链，
      memoryd 重启后改用 gateway，存量向量因 `model` 变化被判 stale 并在
      60 s 内后台重建完成。结果与验收证据见 §8.2；回滚方式见第 9 节。
- [x] 清理 K3 上的退役残留（旧模型副本 + `embedding.env`，2026-10-12；见 §11.5）

## 11. 链路速览：下载与推理的归属（2026-10-12 补充）

§6 / §7 讲的是设计与实施步骤，本节只收口一件事：`qwen3-embedding-0.6b-q8_0`
的**下载**、**加载**与**推理**分别经过谁，以及运行期 memoryd 到底发出了什么。

### 11.1 运行期（memoryd 只发一个 HTTP 请求）

```text
bianbu-agent-memoryd（Rust，进程内零 HTTP 运行时依赖）
  │
  ├─ 查询侧  long_term.search_headers      (crates/memory-store/src/long_term.rs:364)
  │    └─ Store::semantic_stage            (crates/memory-store/src/embedding.rs:1356)
  │         · 加 query_instruct 前缀，预算 min(max_latency_ms, 1500 ms)
  ├─ 写入侧  put/revise 事务提交后
  │    └─ index_memory_after_commit        (embedding.rs:1171)
  │         └─ index_with_config           (embedding.rs:1182)   500 ms 预算
  └─ 后台    run_embedding_backfill        (crates/memory-store/src/background.rs:142)
       · 60 s 周期，失败按退避重试，预算 index_timeout_ms
  │
  │ 三条路径共用同一个客户端
  ▼
EmbeddingClient (embedding.rs:106；new:118，embed_texts:136)
  · 手写 TcpStream + HTTP/1.1，不引入 HTTP 库，调用在 SQLite 事务之外
  · 出站前按 token 预算裁剪（clip_to_token_budget:363，留头 75% + 尾 25%）
  · 请求体 {"model": <gateway 侧模型 id>, "input": [...]}
  │
  ▼
POST http://127.0.0.1:18790/v1/embed/embeddings      ← 运行期唯一接口
  │
  ▼
spacemit-ai-gateway Embed 域（由 spacemit-ai-gateway.service 持有，User=bianbu）
  · 按请求里的 model 字段路由，不依赖 _current_model 指针
    ├─ bibit-embed               → 127.0.0.1:18800（Q4_0，意图模块，原样不动）
    └─ qwen3-embedding-0.6b-q8_0 → 127.0.0.1:18801（Q8_0，本模块）
  │
  ▼
llama-server（gateway spawn，不是 memory 启动）
  -m ~/.cache/models/embed/Qwen3-Embedding-0.6B-Q8_0.gguf
  --embedding --threads 8 --metrics
  --pooling last --embd-normalize 2 --ctx-size 4096 --parallel 1
  │
  ▼
1024 维向量 → 响应校验（条数 / index / 维度 / 有限值 / <16 MiB / 批 ≤128）
  ├─ 查询侧：RRF(k=60, embedding.rs:64) 与 FTS 级联融合
  │          （fuse_search_results:571、fuse_semantic_stage:660）
  └─ 写入侧：int8 量化后 upsert（persist_embedding:1262）
```

失败一律静默降级：`Unavailable`（未启用 / 查询过短 / 端点不可用）与 `Degraded`
（试过但失败）都返回词法结果，差别只在能否进热查询缓存（MEM-NFR-02）。

### 11.2 安装期（provisioner 只负责「让模型就位并钉住参数」）

```text
bianbu-agent-memoryd.postinst ──(systemctl --no-block start)──►
bianbu-agent-memory-embedding-provision.service（oneshot，After=spacemit-ai-gateway）
        │
        ▼  /usr/libexec/bianbu-agent-memory-embedding-provision
   GET  /v1/embed/healthz                    网关不在 → 记一行日志，exit 0（非致命）
   GET  /v1/embed/models                     该 id 不存在 → 记日志，exit 0
   POST /v1/embed/models/{id}/download       + GET 轮询进度（有界 1800 s）
   POST /v1/embed/models/unload              已加载则先停，好让下次 load 决定参数
   POST /v1/embed/models/load                extra_args 钉死启动参数
   金样自检 POST /v1/embed/embeddings        6 条中文文本最低余弦 ≥ 0.999 才放行
        │
        ▼
   journal 一行「verified Gateway model at <local_path>」
```

### 11.3 归属表

| 环节 | 承担方 | 证据 |
| --- | --- | --- |
| GGUF 下载（`archive.spacemit.com`） | spacemit-ai-gateway | `embed.models[].url`（`/opt/spacemit-ai-gateway/configs/base.yaml:163`）；provisioner 只触发 download 并轮询进度 |
| 模型落盘位置 | spacemit-ai-gateway | `~/.cache/models/embed/`（`embed.storage.models_dir`）；网关自身 `embed/db.sqlite` 记 `local_path` |
| 推理进程的启动 / 常驻 / 回收 | spacemit-ai-gateway | 进程树 `spacemit-ai-gateway` → `llama-server --port 18801`，属主 `bianbu` |
| 推理请求与向量返回 | spacemit-ai-gateway | 运行期唯一接口 `POST /v1/embed/embeddings` |
| 加载参数（`--pooling` / `--ctx-size` / `--parallel` / `--embd-normalize`） | bianbu-agent-memory（钉死） | provisioner 的 `extra_args`，理由是 `embed.default_args` 会被 bibit deploy 脚本改写（§2.1、§7.3） |
| 端点与模型 id 配置 | bianbu-agent-memory | `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL`（`crates/memory-store/src/product.rs:21`） |
| 文本裁剪、量化、存储、融合、降级 | bianbu-agent-memory | `crates/memory-store/src/embedding.rs` |

一句话：**下载与推理是 gateway 的能力，加载参数是 Memory 的决定**。所有权这样分开，
正是为了在 bibit 重新部署改写共享默认参数时，Memory 的向量空间不会在维度不变的前提下
被换掉。

### 11.4 运维命令

```bash
# 上一次 provisioning 的结果（含金样自检与已验证模型路径）
journalctl -u bianbu-agent-memory-embedding-provision.service -n 10 --no-pager
# gateway 重启后把加载参数钉回去（自带 unload + load）
sudo systemctl restart bianbu-agent-memory-embedding-provision.service
# 只看状态、不做任何变更
sudo /usr/libexec/bianbu-agent-memory-embedding-provision --check
# 网关侧模型清单与加载态
curl -s http://127.0.0.1:18790/v1/embed/models
```

### 11.5 退役残留清理记录（2026-10-12，K3 192.0.2.10）

下面两处自 0.1.17 起不再被任何组件读取，包也**不会**自动删除（`postinst` 明确
保留它们），确认网关侧模型可用后手工清理。清理前核查：无进程持有（`fuser` 为空）、
已安装代码中无任何引用。

| 路径 | 内容 | 处置 |
| --- | --- | --- |
| `/var/lib/bianbu-agent-memory/models/Qwen3-Embedding-0.6B-Q8_0.gguf` | 旧自管推理栈的模型副本（639 MB） | 已删除；`models/` 目录为空后一并移除 |
| `/etc/bianbu-agent-memory/embedding.env` | 旧推理 unit 的 `PARALLEL=1` 参数 | 已删除 |

清理后复核：`bianbu-agent-memoryd` / `spacemit-ai-gateway` /
`bianbu-agent-memory-embedding-provision` 三个 unit 均 `active`；
`POST /v1/embed/embeddings` 仍返回 1 条 × 1024 维；Q8_0 推理进程仍由 gateway 持有
并运行（用的是网关自己那份 `~/.cache/models/embed/` 副本，不受影响）。

> `/var/lib/bianbu-agent-memory/memory.config.json.bak-*` 是配置备份，与本次退役无关，
> 未做处理。
