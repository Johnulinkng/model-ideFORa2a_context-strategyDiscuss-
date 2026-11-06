# 复杂任务检查点

当前主助手自动选择内容并调用，用户无需填写表格。`complexity_reason` 应说明实际复杂性，不能为了触发咨询编造理由。简单任务不使用；工具收到 `complexity: "simple"` 时跳过，不调用模型。

资料包示例：

```json
{
  "complexity": "complex",
  "complexity_reason": "涉及网关兼容、阶段状态与结束续跑，错误会导致漏复核或反复调用。",
  "stage": "milestone",
  "original_request": "用户最初想达成什么，保留必要原话。",
  "context": "必要前情、后续修正、目标和关键约束，几句话能讲清就保持简短。",
  "progress": "已经做了什么，出现什么结果，有哪些尚未证实；不要把拟执行写成已完成。",
  "current_result": "当前方案、阶段成果或拟交付结果的实际内容，包含审查所需片段；不能只给本地路径。",
  "codex_assessment": "当前Codex的结论、见解和依据，区分已证实事实与假设。",
  "acceptance": ["已执行的验证及结果", "尚缺的验收及原因"],
  "constraints": ["必要约束"],
  "evidence": [{"source": "可核对的来源或测试命令", "content": "必要内容或结果片段"}],
  "previous_review": "上一轮主要意见、采纳或不采纳的理由；首轮可省略。",
  "open_questions": ["本轮需要讨论的分歧；没有可省略"],
  "review_question": "用户本轮明确补充的问题；没有就省略，不自动添加细致评价清单。",
  "research": {
    "required": true,
    "queries": ["site:api-docs.deepseek.com Responses web_search deepseek-flash"],
    "freshness_key": "2026-11-06/official-tool-support"
  }
}
```

`stage` 为 `plan`（方案）、`milestone`（阶段成果）或 `delivery`（准备交付）。复杂度理由、原始请求、上下文、过程、当前成果和非空验收列表必须提供。`codex_assessment` 为旧包兼容而可省略，新包应填写。`research` 只在需要外部取证时填写，纯内部检查省略。压缩由当前主助手完成；脚本只校验、打包，不再调用另一个模型压缩，也不自动读取证据路径。

`research` 最多两个公开短查询，每条 1–128 Unicode 字符；不要包含内部资料。`freshness_key` 用当前证据批次／日期表达本轮时效要求，需新证据时更新，不把旧缓存称为刚刚搜索。取证经官方 `/v1/tools/search`，固定每条最多 3 个结果，要求正文内容；不切换 Flash 型号。搜索失败不调用后续 DS 假装已查；零结果可以带“未获取来源”继续复核，但不能据此宣称外部标准达成。

结果编号 `[E1]` 等，保存查询、来源 URL、页面日期（若有）、检索时间、完整原始文本长度／哈希及摘录标记。`query-paragraph-v1` 根据当前公开查询选取命中正文、所属标题及邻文；正文最多 12000 UTF-8 字节，摘要最多 4000。片段合并、去重后按原顺序输出，间隔用 `[... omitted ...]` 明示；`*_selection.spans` 是完整上游原文的 UTF-8 字节区间。`status=matched` 仅为词法命中，`omitted_matching_blocks` 表示还有命中块未选，`truncated` 表示存在块内截取；不能据此宣称完整语义或整页已验证。`no_match` 的头部回退只是背景，`empty` 说明没有正文。必要条款未见则报告证据不足或继续取证。来源的排名分数不等于标准的权威性。追加证据后仍须符合 256 KiB 预算，不得裁剪 `current_result`。选段策略纳入缓存指纹，旧头部回执不能当作新策略已复核。

默认主问句参考用户的表达：

> 这里是目前的上下文、我的验收结果和最开始的请求，你看看这个结果是否能满足我最开始的请求意图；如果按照业界标准，这些结果／指标是否已经达成；这里的见解和结论是否有问题。如果这里有缺任何上下文信息请询问我。请用较为简洁的表达回答。

不再追加条数、固定格式或指标清单。额外 `review_question` 只用于用户本轮明确补充的问题。收到 DS 意见后，由当前 Codex 结合原始目标和实际证据自行判断下一步，不将 DS 的回复直接当成验收裁决。

调用顺序：

```sh
python3 <SKILL_DIR>/scripts/review_checkpoint.py register --request-file /absolute/checkpoint.json
python3 <SKILL_DIR>/scripts/review_checkpoint.py review --request-file /absolute/checkpoint.json --output /absolute/ds-result.json
```

登记不联网；首次送审调用一次现有复核工具。输出含检查点 ID、当前成果 SHA-256、DS 原文及 `consultation.round/max_rounds`。DS 若请求缺失上下文，由当前 Codex 查证并准备简短补充文件，不要求 DS 使用固定回复格式：

```json
{"context_request":"DS希望补充什么，保留必要原话，说明为什么需要补。","supplement":"Codex从授权上下文或工具中取得的事实及必要内容；未查到的明确说明。"}
```

```sh
python3 <SKILL_DIR>/scripts/review_checkpoint.py followup --checkpoint-id RETURNED_ID --request-file /absolute/supplement.json --output /absolute/ds-round2.json
```

`followup` 沿用原始包和成果，不以新检查点重置计数；自动带上首轮顾问原文、询问原因与补充。原包、首轮和第二轮回文分别留档；检索资料在第二轮复用，不重复搜索。第二轮后不同补充被拒绝；完全相同的已完成补充只读缓存。主助手确实需要新检索或有实质新成果时才开启新阶段，不能为重复追问重置上限。两轮仍不能确定就如实说明限制，由 Codex 判断继续查事实、等待必要用户信息或交付，不再向 DS 追问。

CLI 同会话调用用非阻塞文件锁串行化，忙时返回 `checkpoint_busy`，不启动并行重复咨询；Hook 可立即标记中断，迟到回复不能恢复已失效的检查点。外部请求崩溃或失败不代表已取得意见，不能据此交付已复核的结论。

主助手读完一轮或两轮意见后准备判断文件：

```json
{"decision":"采纳哪些、暂不采纳哪些及证据；接下来做什么验证。","action":"continue"}
```

```sh
python3 <SKILL_DIR>/scripts/review_checkpoint.py decide --checkpoint-id RETURNED_ID --decision-file /absolute/decision.json
```

`action` 为 `continue` 或 `deliver`；只有 `delivery` 检查点能记为 `deliver`，并且必须已有成功复核。记录判断不等于完成后续测试；操作仍要遵守原权限。成果实质修改后用新资料包再复核。同版缓存同时绑定资料包及非敏感路由指纹（模型别名、网关配置路径和工具路径），这些配置变化会新建检查点；指纹不读取凭据内容。它不监视同一路径内部的配置变化，主助手确认上游或目标模型实质变化时也应更新资料包，重新咨询。

状态只在当前会话、工作目录和配置作用域内生效。新用户消息使旧待处理状态失效；Stop 自己产生的同检查点续跑保留状态。用户中断变为 `interrupted`。Stop 只检查显式 `pending` 或 `reviewed` 状态，每检查点最多发一次续跑要求；`reviewed` 表示 DS 已返回，`decided` 表示主助手已记录判断。它不能识别未登记的工作。

审阅 `hooks/checkpoints.json` 后，可在桌面 Settings → Hooks（或 CLI `/hooks`）信任。此步骤是官方产品要求；在信任并观察真实事件前，只报告脚本模拟通过。官方约定见 https://learn.chatgpt.com/docs/hooks 。
