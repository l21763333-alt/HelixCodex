---
name: forecast-feishu-review-card
description: 将实验报告精炼为飞书人工审批卡片。当实验完成需要人类专家决策时使用。输入 final_report.md 和 metric_comparison.json，输出紧凑的飞书消息卡片。
---

# 目的

把冗长的实验报告压缩为专家可快速阅读、可做出决策的飞书卡片，保留决策所需的全部关键信息，去掉冗余叙述。

# 输入

- `{output_dir}/final_report.md` — T4 完整报告
- `{output_dir}/evaluation/metric_comparison.json` — 指标对比（数值归这里）
- `{output_dir}/agent1/experiment_plan.yaml` — 实验计划（改动从这里取）
- `{output_dir}/agent2/review_result.json` — 确定性决策（不可修改）

# 工作流

1. 读取 `review_result.json`，提取 decision（keep/rollback）和 reason
2. 读取 `metric_comparison.json`，提取 primary 和 secondary 指标
3. 读取 `experiment_plan.yaml`，提取 changes 列表
4. 读取 `final_report.md` 的风险与注意事项章节
5. 按输出格式生成卡片

# 输出格式

输出一段飞书 Markdown，严格不超过以下体积限制：
- 总行数 ≤ 40 行
- 改动要点 3-5 条，每条 ≤ 50 字
- 风险提示 1-3 条，每条 ≤ 40 字
- 指标数值保留 4 位小数

```markdown
🔬 **实验完成: {trial_id}**

**决策建议: {KEEP / ROLLBACK}** | 来源: {auto / human}

---

**核心指标 (package_detail)**

| 指标 | Baseline | New | Delta |
|------|----------|-----|-------|
| WAPE | {old} | {new} | {delta} |
| Bias | {old} | {new} | {delta} |

辅助: store_dish_day WAPE {old} → {new}

---

**主要改动**

1. {改动要点1}
2. {改动要点2}
3. {改动要点3}

---

**⚠ 风险**
- {风险1}
- {风险2}

---

**请回复指令:**
`/keep` 保留此版本，提交代码
`/rollback` 放弃，回到上一版本
`/revise <修改建议>` 调整后重试
`/branch A方向; B方向` 多方向并行
`/stop` 停止实验循环
```

# 规则

- decision 直接从 `review_result.json` 引用，不可修改
- 指标数值从 `metric_comparison.json` 读取，不要手写
- 改动要点从 `experiment_plan.yaml` 的 changes 中提取，用自己的话精炼为一句
- 只写 final_report.md 中明确标注的风险，不要自行新增
- 不要写"本次实验验证结论为"之类已经在 final_report.md 中的重复叙述
- 消息总长度控制在可在飞书一屏内看完
- 不要输出 JSON
- 不要输出代码块
- 直接输出飞书 Markdown 卡片文本

# 质量检查

- [ ] 指标数字与 metric_comparison.json 完全一致
- [ ] decision 与 review_result.json 完全一致
- [ ] 改动条目数 ≤ 5
- [ ] 风险条目数 ≤ 3
- [ ] 总行数 ≤ 40
