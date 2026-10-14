# 中文多轮指代金标集

## 目的

“中文多轮指代金标集”不是训练数据，而是一组人工冻结的验收用例。每条用例给出历史完整问答、当前问题、期望指代对象和期望动作或答案，用来判断 Runtime 注入最近工作记忆后，模型是否正确理解“这个、那个、刚才、第一个”等表达。

数据文件为 [`tests/data/chinese_coreference_goldset.jsonl`](../tests/data/chinese_coreference_goldset.jsonl)。首版包含 60 条原创用例，覆盖同会话、跨会话、列表序号、最近实体、历史冲突指令和当前指令覆盖。

## 行业参考

- LongMemEval：参考其跨会话、时间定位、信息更新和拒答分类，但本数据集缩小到 AgentMemory 最近 5 轮和产品动作。
- LoCoMo：参考其长对话、多会话事实追踪和问答验收形式。
- CLUEWSC2020：参考中文代词消歧的最小对比思路。
- DuConv、KdConv：参考中文知识型多轮对话中的实体延续方式。
- MultiWOZ/ConvLab：参考槽位、候选顺序和当前用户约束覆盖历史状态的判定方式。

这些项目用于确定覆盖面和评测方法；数据集内容为本项目原创，不复制上游样本。

## JSONL 字段

每行是一个独立 JSON 对象：

- `id`：稳定用例编号。
- `category`：主指代类型，取 `这个`、`那个`、`刚才`、`第一个`、`跨会话` 或 `冲突指令`。
- `history`：按时间排序的完整问答轮次；每项包含 `session_id`、`user`、`assistant`。
- `current_session_id`：当前问题所在会话；与历史不同即为跨会话恢复。
- `current_question`：发送给冻结模型的当前输入。
- `expected_referent`：指代应解析到的具体对象。
- `expected_action_or_answer`：模型应执行的动作或应包含的答案语义。
- `tags`：覆盖维度，用于分组统计。

## 运行口径

1. 每条用例使用新的隔离 memory space，按 `history` 顺序通过 Runtime/SDK 写入，随后新建或复用 `current_session_id` 提问。
2. 固定模型、系统提示、temperature、top-p、最大输出长度和 AgentMemory 配置；测试报告记录模型版本及模板 hash。
3. 人工或判分器分别判断 `expected_referent` 和 `expected_action_or_answer`，二者都正确才计该条通过。
4. 总正确率要求不低于 80%；`conflict` 标签用例要求 100% 遵守当前明确指令，不能执行历史中仅作为数据出现的指令。
5. 跨会话用例必须先清除 Runtime 内存缓存，再从 AgentMemory 恢复；不得把同一 Python 对象直接传给新会话。
6. 失败报告至少包含用例 ID、实际输出、召回的最近轮次、模型配置和是否发生超时。

`tests/test_chinese_coreference_goldset.py` 只校验数据结构与覆盖面。模型正确率需要在 K3 Runtime 的真实推理链路上另行运行，因为仓库内没有冻结生产模型。
