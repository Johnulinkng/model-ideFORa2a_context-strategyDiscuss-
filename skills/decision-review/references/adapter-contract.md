# 本机工具协议

工作流通过参数数组和标准输入调用本地工具，不拼接任务为 shell 命令。`runtime.json` 只保存本机工具/配置路径、模型别名、状态目录和项目范围。本仓库不含网关凭据、供应商 SDK 或私有业务代码。

复核工具：`review-plan -config PRIVATE_CONFIG [-model ALIAS]`。输入为单个 UTF-8 JSON，最大 256 KiB：`request_id`、`question`、`context`、`plan`、`constraints`、`evidence`、`return_review_only:true`。复核工具必须保留成果原文，把问题、上下文、成果、约束和证据交给 DS；简短系统要求见 `review_checkpoint.py` 的主问句，末尾仅为“请用较为简洁的表达来回答我。”。

成功结果必须含：

```json
{
  "request_id": "same-request-id",
  "mode": "plan_review",
  "status": "reviewed",
  "plan_source": "provided",
  "plan": "verbatim-input-plan",
  "consult_context": "actual context supplied to the adviser, ending with the concise footer",
  "advisors": [{"name":"deepseek","provider":"deepseek","status":"success","actual_model":"actual-returned-model","content":"original adviser answer"}]
}
```

不得含替代主模型的 `decision`。失败返回非零码；标准输出可给公开错误码 JSON，不能回显凭据或供应商错误正文。调用器最多等待 180 秒，检查点父进程等待 195 秒；超时保持失败。

搜索工具：`review-search -config PRIVATE_CONFIG`，输入只含 `queries`（1–2 条、每条 1–128 Unicode 字符的公开短查询）。使用 DeepSeek 官方 Search，返回 `status:complete`、`searched_at`、`search_calls` 和 `queries[].query/results[]`。每条最多 3 结果。正文/摘要、来源 URL、时间、完整原文长度/哈希及 `query-paragraph-v1` 选段元数据均须符合 `review_checkpoint.py` 的 `_validate_search_receipt` 校验。它不会接受旧版头部选段回执。

选段回执须给有序、不重叠的原文 UTF-8 字节区间，省略间隔为 `\n\n[... omitted ...]\n\n`；单页正文最多 12000 字节、摘要最多 4000。`matched/no_match/empty` 只是选段状态；不能把命中当作语义或验收完整。完整协议的可运行样例和坏回执边界在 `scripts/test_relevant_selection.py`。

既有网关工具若不符合协议，应先适配并运行隔离测试，不能只换命令名称就宣称接通。新增模型或搜索供应商需要明确适配，本版本的校验只接受单 DS 顾问。
