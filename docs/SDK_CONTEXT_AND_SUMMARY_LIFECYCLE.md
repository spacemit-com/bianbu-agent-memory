# SDK 上下文与摘要生命周期

## 调用层次

```text
Runtime before_run
  ├─ SDK record_run_started：记录开始水位
  └─ SDK prepare_context：问候/历史/相关记忆/Instruction/L2 策略与组装
       └─ memoryd：读取当前身份可访问的数据
Runtime：将 SDK 返回的 context_sections 注入本轮提示词

Runtime after_run
  └─ SDK record_runtime_result：解析宿主终态、结束标记、压缩结果
       └─ memoryd append_event：保存完整问答，决定周期或结束摘要

memoryd 后台：空闲摘要补偿 → 保留期维护 → 索引维护
```

Runtime 插件不识别问候、不读取称呼槽位、不组装摘要、不扫描其它会话，也不决定何时强制摘要。旧的文件后端同样由 SDK 读写，插件仅转发其数据目录。

## SDK 入口与出口

```python
bridge = await get_memory_bridge(account_id=account_id, agent_name=agent_id)
patch = await bridge.prepare_context(
    query=user_text, session_id=session_id,
    config={"timeout_seconds": 2.0, "recall_top_k": 5, "recall_max_chars": 4000},
)
# patch 为 None 或 {"context_sections": (...,), "metadata": {...}}

recorded = await bridge.record_runtime_result(
    run_id=run_id, session_id=session_id, agent_id=agent_id,
    user_text=user_text, assistant_text=answer, status="completed",
    metadata=trusted_host_metadata,
)
```

桥接实例的身份由宿主提供；不从用户文本或模型参数获取。`prepare_context` 是只读的有界操作：

- `hi/你好/在吗` 等纯问候不查普通记忆，只检查 `profile.preferred_address`；称呼缺失、非法或偏好应用关闭时使用中性问候。
- 业务问题按相关性召回，保留注入防护和偏好/开发习惯开关。
- 历史问题返回使用 `session_search` 的指引，不用长期记忆冒充历史原文。
- 独立读取有效 Instruction；当前会话 L2 只取摘要，显式 `include_events=False`。摘要兼容规则和模型的字段名称，组装内容最多4000字符。
- 记忆关闭时不注入任何记忆内容；普通异常/超时降级，任务取消继续传播。

`record_runtime_result` 解析可信宿主的 `session_end/conversation_end/close_session/finalize_session` 标记，并通过完成的 assistant 事件将结束信号传给 memoryd。调用方不得将模型自造的压缩信息或结束标记当作宿主事件。没有结束信号时不凭切换会话判定关闭。

## memoryd 摘要策略

- 常规触发仍为配置的完整问答轮数，默认5轮。SDK 不再重复发起 summarize 或逐轮保留期维护。
- 完成的 assistant 事件带显式结束信号时，memoryd 强制补齐已完成问答的摘要；单独用户消息不能绕过完整问答检查。
- 后台每60秒检查一次空闲候选，普通会话静默至少60秒后补摘要；每个身份命名空间每批最多8个候选。不因切换会话写入“已关闭”状态。
- 未结束的 task_started 水位暂缓补偿；静默达到10分钟后允许对已完成问答做快照，避免崩溃遗留水位永久阻塞。未完成问答仍不构成摘要轮次。
- 后台从持久化 L1 和摘要覆盖序号判断，重启后可恢复；候选不足、无完整配对或已覆盖时不生成新摘要。扫描进度和失败重试持久化，避免无效旧数据占住全部批次。
- 补摘要不删除原始事件，不跨 account/agent/memory_space；记忆关闭时跳过。原始事件仍按原保留期策略处理。

正常负载下空闲后约60～120秒可补摘要；候选积压、后台重试或未结束任务会延后。后台被禁用时没有自动空闲补偿，因此生产服务必须保留 `--background-device-id` 和后台线程。

## 配套升级

新增 SDK 方法、精简插件和 memoryd 应一起部署。新 SDK 将结束信号交给 memoryd；旧 memoryd 不识别它，不能承诺即时结束补摘要。schema 14 只扩展后台调度表，保留已有计划和重试记录，不改问答或长期记忆的数据格式。

旧 SDK 和显式 `sessions.summarize` 接口仍可使用；新 Runtime 插件依赖新 SDK 方法。升级前保存数据库和二进制备份；回滚整套时同时考虑数据库迁移与现场备份。
