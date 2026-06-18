# ForeCast by Codex Flow

基于 OpenAI Codex SDK 的预测实验自动诊断与优化工作流。

输入预测实验目录 + 优化目标 → 自动完成：**诊断 → 计划 → 代码生成 → 训练验证 → 报告**。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 运行 (设备码登录)
python codex_flow.py \
  --experiment /path/to/forecast_exp \
  --ask "分析预测误差，提出特征实验并验证" \
  --output runs/trial_001

# 或 API Key 登录 (推荐, 无过期问题)
export OPENAI_API_KEY="sk-..."
python codex_flow.py \
  --experiment /path/to/forecast_exp \
  --ask "分析预测误差，提出特征实验并验证" \
  --output runs/trial_001
```

## 架构

```
1 个 Codex Session → 4 LLM Threads + 1 Python 确定性执行

T1  Evaluate  Codex LLM  扫描/日志/badcase/优化建议/标准化
T2a Plan      Codex LLM  特征假设 + 实验计划
T2b Codegen   Codex LLM  代码生成 + 自验证(py_compile+冒烟)
T3  Execute   Python     训练 → 评测 → keep/rollback 决策
T4  Report    Codex LLM  实验验证结论报告
```

## 实验目录要求

```
experiment/
├── src/        # 训练源码
├── data/       # 训练数据
└── outputs/    # 基线预测输出 (含 split 列 + pred/true 列)
```

## Skills

`skills/` 目录包含 10 个 forecast 专用 skill，每个 skill 通过 `SKILL.md` 定义执行规则：

| Skill | 用途 |
|-------|------|
| `using-forecast` | 流程调度器，决定调用哪些 skill 及顺序 |
| `forecast-task-planner` | ask → 结构化任务意图 |
| `forecast-experiment-scanner` | 实验目录扫描与产物发现 |
| `forecast-code-log-analyzer` | 代码结构与训练日志分析 |
| `forecast-evaluation-analyzer` | WAPE/Bias/MAE/RMSE 指标计算 |
| `forecast-badcase-locator` | badcase 挖掘 (高误差/连续偏差) |
| `forecast-optimization-advisor` | 基于证据的优化建议 |
| `forecast-optimization-case-reference` | 可迁移的案例模式 |
| `forecast-trial-codegen` | 代码生成规则 (14 条硬约束 + 自验证) |
| `forecast-report-writer` | 中文评测报告撰写 |
