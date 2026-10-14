# 外部 API 契约验收 Demo

`scripts/external_api_contract_demo.py` 从外部调用方视角验证 AgentMemory 的 SDK/Runtime/模型接口，并将证据分成两级：

- `isolated_real_sdk_and_daemon`：真实 AgentMemory SDK 调用隔离的 SQLCipher memoryd，可作为内部接口闭环证据。
- `mock_contract`：生产 Adapter 调用本地 Mock 外部服务，只证明请求/响应和失败处理契约正确。
- `live_external`：调用真实 Runtime 或真实模型服务，可用于对应外部联调行的验收证据。
- `SKIP_EXTERNAL_RESOURCE`：资源未提供，必须保持规格进行中/未开始，不能按通过处理。

## 默认运行

```bash
PYTHONPATH=sdk/python/src \
python3 scripts/external_api_contract_demo.py \
  --memoryd target/release/bianbu-agent-memoryd \
  --runtime-url http://127.0.0.1:9900
```

默认启动临时加密 memoryd，模拟 BiBit/Runtime 调用设置、L3 词典、长期记忆、逐轮持久化、最近上下文、默认账号同步/备份拒绝；模型部分使用本地 Mock，但调用的是真实 `OpenAICompatibleMem0Planner`。

## 真实模型接口

```bash
export BIANBU_CONTRACT_MODEL_TOKEN='replace-with-test-token'
PYTHONPATH=sdk/python/src \
python3 scripts/external_api_contract_demo.py \
  --memoryd target/release/bianbu-agent-memoryd \
  --runtime-url http://127.0.0.1:9900 \
  --model-base-url http://127.0.0.1:8080/v1 \
  --model-name qwen2.5-1.5b-instruct-q4_k_m \
  --model-api-key-env BIANBU_CONTRACT_MODEL_TOKEN
```

Token 只从环境变量读取，不写入报告。该调用可验证 OpenAI-compatible 请求、鉴权和结构化输出，但 `MEM-INT-08` 仍需运行完整 60 条指代集和冻结摘要集才能闭环。

使用 `--require-live-external` 时，任何 Security、BiBit、Cloud、第二设备或真实模型资源缺失都会让 Demo 返回失败；默认模式允许缺失，并在 JSON 报告中明确列为 `SKIP_EXTERNAL_RESOURCE`。

## 可闭环边界

Demo 可作为以下内部部分的证据：

- BiBit/Runtime 调用 AgentMemory 设置、L3、L1/L2 API 的契约可用；
- `default` 账号同步和备份失败关闭；
- Runtime 页面健康入口可访问；
- OpenAI-compatible 模型 Adapter 的请求/响应契约。

Demo 不能单独闭环真实 BiBit UI、正式账号/Token 生命周期、Secret Broker、生产签名、SEC-06、云文件存储、双设备恢复或模型质量指标。
