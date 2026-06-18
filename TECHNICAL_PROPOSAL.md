# ComboScope Codex Flow — 技术方案与演进路线

> 版本: v2.0  
> 日期: 2026-06-18  
> 状态: 规划中

---

## 目录

1. [总体架构](#1-总体架构)
2. [架构分层详解](#2-架构分层详解)
3. [当前已实现功能](#3-当前已实现功能)
4. [未来优化方向](#4-未来优化方向)
5. [实施排期](#5-实施排期)
6. [风险与应对](#6-风险与应对)
7. [附录](#7-附录)

---

## 1. 总体架构

### 1.1 完整技术框架图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         codex_flow_config.json                               │
│              统一配置: 飞书 / Git / 人工审批 / 记忆注入 / 循环               │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
        ┌───────────────┬───────────┼───────────┬───────────────┐
        ▼               ▼           ▼           ▼               ▼
┌───────────┐   ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐
│  Lark     │   │   Git     │ │  Memory   │ │ codex_    │ │  Human    │
│  MCP      │   │   MCP     │ │  Manager  │ │ flow.py   │ │  Review   │
│  Server   │   │   Server  │ │           │ │ (编排引擎) │ │  (飞书)   │
└─────┬─────┘   └─────┬─────┘ └─────┬─────┘ └─────┬─────┘ └─────┬─────┘
      │               │             │             │             │
      ▼               ▼             ▼             ▼             ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          集成总线 (Integration Bus)                          │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                        codex_flow.py 核心编排                        │   │
│  │                                                                     │   │
│  │  T1 诊断          T2a 计划         T2b 代码生成                      │   │
│  │  ├─ scanner       ├─ advisor       ├─ codegen skill                  │   │
│  │  ├─ analyzer      ├─ case-ref      ├─ py_compile 验证                │   │
│  │  ├─ badcase       ├─ planner       ├─ 冒烟测试                       │   │
│  │  └─ advisor       └─ report-writer └─ 14 条硬约束                    │   │
│  │                                                                     │   │
│  │  T3 训练评估                       T4 报告                           │   │
│  │  ├─ subprocess train              ├─ report-writer                  │   │
│  │  ├─ WAPE/Bias 确定性计算           ├─ final_report.md               │   │
│  │  ├─ keep/rollback 决策            └─ 产物归档                        │   │
│  │  └─ codegen retry ×2 + 兜底                                       │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                        │
│    ┌───────────────┬───────────────┼───────────────┬───────────────┐       │
│    ▼               ▼               ▼               ▼               ▼       │
│ ┌──────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐      │
│ │Lark  │   │Git       │   │Memory    │   │Skills    │   │Runs      │      │
│ │飞书  │   │版本控制  │   │记忆管理  │   │10 Skills │   │实验产物  │      │
│ └──────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘      │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 核心设计原则

```
┌────────────────────────────────────────────────────────────────┐
│                    职责分离原则                                 │
├──────────────┬─────────────────────────────────────────────────┤
│ 模块         │ 一句话职责                                       │
├──────────────┼─────────────────────────────────────────────────┤
│ codex_flow.py│ 管"实验怎么跑"                                   │
│ Lark MCP     │ 管"人怎么决策"                                   │
│ Git MCP      │ 管"代码怎么沉淀"                                 │
│ Memory Mgr   │ 管"经验怎么记住"                                 │
│ Skills       │ 管"领域知识怎么编码"                             │
│ Config       │ 管"开关怎么控制"                                 │
└──────────────┴─────────────────────────────────────────────────┘
```

---

## 2. 架构分层详解

### 2.1 codex_flow.py — 实验编排引擎

**状态**: ✅ 已实现 (1,420 行)

```
                    ┌─────────────────────┐
                    │   CLI / Loop Mode   │
                    └─────────┬───────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
   │  单次模式    │   │  循环模式    │   │  链式继承    │
   │  --output    │   │  --loop      │   │  --previous- │
   │  runs/trial  │   │  --max-iter  │   │  trial       │
   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
          │                  │                   │
          └──────────────────┼───────────────────┘
                             ▼
              ┌─────────────────────────┐
              │   run_workflow()        │
              │                        │
              │  T1: Evaluate+Diagnose │
              │  T2a: Plan             │
              │  T2b: Code Generation  │
              │  T3: Execute (Python)  │
              │  T4: Report             │
              └─────────────────────────┘
```

#### 关键能力

| 能力 | 实现方式 | 状态 |
|------|---------|------|
| 单次实验 | `run_workflow()` — T1→T2a→T2b→T3→T4 | ✅ |
| 循环优化 | `run_loop()` — 一次认证 N 轮实验 | ✅ |
| 链式继承 | `--previous-trial` — 代码+指标跨轮传递 | ✅ |
| 额度管理 | `_ensure_credits()` — 检测耗尽→等待恢复→重试 | ✅ |
| 失败回退 | codegen retry ×2 → 原始脚本兜底 | ✅ |
| 确定性决策 | WAPE↓≥0.005 & |Bias|↑<0.02 → KEEP | ✅ |

---

### 2.2 Lark MCP Server — 飞书 Human-in-Loop

**状态**: ⏳ 规划中 (当前 `lark_notify.py` 仅单向通知)

#### 目标

飞书不只是通知渠道，而是 **human-in-loop 决策入口**。

#### 暴露工具

```
┌──────────────────────────────────────────────────────────┐
│                  Lark MCP Server Tools                    │
├──────────────────────┬───────────────────────────────────┤
│ 工具名               │ 功能                              │
├──────────────────────┼───────────────────────────────────┤
│ send_experiment_review│ 发送T3评测结果+请求人工决策        │
│ wait_human_feedback  │ 阻塞等待飞书回复 (含超时处理)      │
│ get_feedback_thread  │ 获取指定trial的完整对话线程        │
│ send_status_update   │ 发送阶段性进度更新                │
│ send_error_alert     │ 发送异常告警                      │
│ send_quota_alert     │ 发送额度告警                      │
│ send_final_summary   │ 发送循环终止汇总                  │
└──────────────────────┴───────────────────────────────────┘
```

#### 飞书消息协议

每次 T3 训练完成后，发送结构化 Review 卡片:

```
[AutoResearch Review] trial_024

状态: 训练完成，等待人工判断
建议: REVIEW

指标:
- WAPE: 0.7009 → 0.6429  (-8.3%)
- Bias: +0.1695 → +0.0837  (-50.6%)

主要改动:
- Tweedie objective (variance_power=1.2)
- holiday position features
- group calibration

风险:
- store_dish_day 未明显改善
- group calibration 可能引入业务切片回退

请回复:
/keep 原因
/rollback 原因
/revise 修改建议
/branch 方向A; 方向B
/stop 原因
```

#### 人工反馈结构化解析

飞书 MCP 解析回复后返回标准化 JSON:

```json
{
  "trial_id": "trial_024",
  "decision": "revise",
  "feedback_text": "保留 Tweedie，去掉 group calibration，重点看 store_dish_day",
  "reviewer": "张三",
  "message_id": "om_xxx",
  "received_at": "2026-06-18T12:30:00+08:00",
  "next_action": {
    "parent_trial": "trial_023",
    "instructions": [
      "keep tweedie objective",
      "remove group calibration",
      "focus store_dish_day regression"
    ]
  }
}
```

#### 配置项

```json
{
  "lark": {
    "enabled": true,
    "mcp_server": "lark_research",
    "chat_id": "oc_xxx",
    "review_timeout_hours": 24,
    "default_on_timeout": "pause",
    "commands": ["/keep", "/rollback", "/revise", "/branch", "/stop"]
  }
}
```

#### 反馈状态机

```
                    ┌─────────┐
                    │  T3 完成 │
                    └────┬────┘
                         │
                         ▼
                  ┌──────────────┐
                  │ 发送 Review  │
                  │ 等待反馈...  │
                  └──┬───┬───┬──┘
                     │   │   │
          ┌──────────┼───┼───┼──────────┐
          ▼          ▼   ▼   ▼          ▼
     ┌────────┐ ┌──────┐┌──────┐  ┌────────┐
     │ /keep  │ │/roll ││/rev  │  │/branch │
     │        │ │back  ││ise   │  │        │
     └───┬────┘ └──┬───┘└──┬───┘  └───┬────┘
         │         │       │           │
         ▼         ▼       ▼           ▼
    ┌────────┐┌───────┐┌───────┐  ┌──────────┐
    │Git提交 ││回退到 ││用指令 │  │从同一父  │
    │PR创建  ││父trial││修改后 │  │trial分叉 │
    │更新记忆││不提交 ││重新跑 │  │并行实验  │
    └────────┘└───────┘└───────┘  └──────────┘
```

---

### 2.3 Git MCP Server — 代码版本闭环

**状态**: ⏳ 规划中 (当前无 Git 集成)

#### 目标

Git MCP 负责版本控制，不让 `codex_flow.py` 直接承担复杂 Git 状态管理。

#### 暴露工具

```
┌──────────────────────────────────────────────────────────┐
│                  Git MCP Server Tools                     │
├──────────────────────┬───────────────────────────────────┤
│ 工具名               │ 功能                              │
├──────────────────────┼───────────────────────────────────┤
│ get_repo_state       │ 获取当前仓库状态 (branch/dirty)    │
│ clone_or_update_repo │ 拉取/更新仓库                     │
│ create_trial_branch  │ 创建实验分支                      │
│ diff_trial_changes   │ 生成实验改动 diff 摘要             │
│ commit_trial_result  │ 提交 KEEP 结果                    │
│ push_branch          │ 推送分支到远端                    │
│ create_pull_request  │ 创建 PR (含实验指标摘要)           │
│ tag_best_trial       │ 标记最优实验                      │
│ rollback_to_parent   │ 回退到父分支                      │
└──────────────────────┴───────────────────────────────────┘
```

#### Git 操作流程

```
实验前                         实验中                     人工决策后
───────                        ───────                    ──────────

┌──────────────┐          ┌──────────────┐          ┌──────────────┐
│get_repo_state│          │diff_trial_   │   /keep  │commit_trial_ │
│              │          │changes       │────────▶│result        │
└──────┬───────┘          │              │          │push_branch   │
       │                  │发 diff 摘要   │          │create_PR     │
       ▼                  │到飞书 Review  │          │tag_best      │
┌──────────────┐          └──────────────┘          └──────────────┘
│create_trial_ │
│branch        │                 /rollback          ┌──────────────┐
│              │────────────────────────────────▶   │rollback_to_  │
└──────────────┘                                    │parent        │
                                                    │不提交改动    │
       /branch                                      └──────────────┘
  ┌─────────────────┐
  │ 从同一父 trial   │
  │ 创建多个 branch  │
  │ 并行跑实验       │
  └─────────────────┘
```

#### 分支命名规范

```
research/trial-024-tweedie-holiday
research/trial-025-remove-group-calibration
research/trial-026-branch-holiday-only
```

#### Commit Message 规范

```
research(trial_024): keep tweedie holiday features

Decision: KEEP
Human feedback: 保留 Tweedie，继续细化节假日特征
WAPE: 0.7009 -> 0.6429
Bias: +0.1695 -> +0.0837
Artifacts:
- runs/trial_024/final_report.md
- runs/trial_024/evaluation/metric_comparison.json
```

#### 安全规则

| 操作 | 权限 |
|------|------|
| `status / diff / log / rev-parse / branch --show-current` | 默认允许 |
| `commit / push / create PR / rollback` | 需人工确认 |
| `reset --hard` | 禁止 |
| `clean -fd` | 禁止 |
| `force push` | 禁止 |
| 直接修改 `main/master` | 禁止 |

#### 配置项

```json
{
  "git": {
    "enabled": true,
    "mcp_server": "git_research",
    "repo_path": "baseline",
    "remote": "origin",
    "base_branch": "main",
    "trial_branch_prefix": "research/",
    "require_human_approval_for_push": true,
    "allow_force_push": false,
    "allow_reset_hard": false
  }
}
```

---

### 2.4 Memory Manager — 长短期记忆管理

**状态**: ⏳ 规划中 (当前无记忆系统，硬规则散落在 prompt 中)

#### 记忆三层模型

```
┌─────────────────────────────────────────────────────────────┐
│                      记忆体系                                │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              长期记忆 (Long-Term)                     │   │
│  │  "绝不能再犯的规则"                                    │   │
│  │  memory/long_term_memory.json                        │   │
│  │  - hard_rules: 绝对禁止模式                          │   │
│  │  - stable_lessons: 经多次验证的稳定经验               │   │
│  └────────────────────────┬────────────────────────────┘   │
│                           │ 每次注入                        │
│  ┌────────────────────────▼────────────────────────────┐   │
│  │              短期记忆 (Short-Term)                    │   │
│  │  "最近做过什么、结果如何"                               │   │
│  │  runs/experiment_memory.json                         │   │
│  │  - recent_trials: 最近N轮实验摘要                     │   │
│  │  - latest_human_instruction: 最新人工指令             │   │
│  │  - open_questions: 待解决问题                         │   │
│  └────────────────────────┬────────────────────────────┘   │
│                           │ 按需注入                        │
│  ┌────────────────────────▼────────────────────────────┐   │
│  │              场景记忆 (Scenario)                      │   │
│  │  "不同业务场景下什么有效"                               │   │
│  │  memory/scenario_memory.json                         │   │
│  │  - holiday: 节假日场景经验                            │   │
│  │  - cold_start: 冷启动场景经验                         │   │
│  │  - store_dish_day: dish级指标经验                     │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              实验记录 (Experiment Record)              │   │
│  │  "每轮实验的完整档案"                                   │   │
│  │  runs/trial_xxx/experiment_record.json               │   │
│  │  runs/experiment_index.jsonl  (快速检索)              │   │
│  │  runs/human_feedback_log.jsonl (反馈流水)             │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

#### 长期记忆结构

`memory/long_term_memory.json`:

```json
{
  "hard_rules": [
    {
      "id": "no_eval_label_leakage",
      "rule": "绝对不允许使用 evaluation-only labels 或 post-outcome 字段构造训练特征",
      "severity": "block",
      "source": "manual",
      "since": "2026-06-18"
    },
    {
      "id": "no_pd_to_numeric_scalar_fillna",
      "rule": "禁止使用 pd.to_numeric(df.get(col, 0)).fillna()，必须使用 numeric_column_or_default",
      "severity": "block",
      "source": "trial_023_codegen_fix",
      "since": "2026-06-17"
    },
    {
      "id": "no_direct_baseline_write",
      "rule": "禁止写入 baseline 原始目录，只能写入 runs/trial_xxx",
      "severity": "block",
      "source": "manual",
      "since": "2026-06-18"
    },
    {
      "id": "output_must_contain_split_column",
      "rule": "预测输出 CSV 必须包含 split 列 (train/valid/test)，否则 T3 指标计算失败",
      "severity": "block",
      "source": "trial_028_failure",
      "since": "2026-06-18"
    }
  ],
  "stable_lessons": [
    {
      "id": "tweedie_for_high_variance",
      "pattern": "tweedie objective",
      "lesson": "对高方差 package demand 有稳定收益，可作为候选方向",
      "evidence": ["trial_023", "trial_025"],
      "confidence": "high"
    },
    {
      "id": "holiday_relative_position",
      "pattern": "holiday_position feature",
      "lesson": "节假日相对位置特征优于简单 binary 标记",
      "evidence": ["trial_023"],
      "confidence": "medium"
    }
  ],
  "promotion_threshold": {
    "auto_promote_if_kept": 2,
    "manual_review_required": true
  }
}
```

#### 短期记忆结构

`runs/experiment_memory.json`:

```json
{
  "best_trial": "trial_025",
  "best_wape": 0.6206,
  "recent_trials": [
    {
      "trial_id": "trial_023",
      "decision": "keep",
      "changes": ["tweedie", "holiday_position", "package_activity", "group_calibration"],
      "metric_effect": "WAPE -0.058, Bias -0.0858",
      "human_feedback": "方向有效，继续细化节假日和供给可得性"
    },
    {
      "trial_id": "trial_025",
      "decision": "keep",
      "changes": ["enhanced_package_activity", "lifecycle_bucket_interaction"],
      "metric_effect": "WAPE -0.0223, Bias +0.0031",
      "human_feedback": null
    },
    {
      "trial_id": "trial_028",
      "decision": "rollback",
      "changes": ["output_format_fix"],
      "failure_reason": "输出CSV缺少split列，评测失败",
      "human_feedback": null
    }
  ],
  "latest_human_instruction": "保留 Tweedie，去掉 group calibration，重点看 store_dish_day",
  "open_questions": [
    "store_dish_day 是否应作为强约束指标",
    "节假日切片是否仍有残余高估",
    "group_calibration 究竟对哪些业务切片有负面影响"
  ],
  "rejected_patterns": [
    {
      "pattern": "group_calibration",
      "reason": "业务切片回退，人工拒绝",
      "trial": "trial_024"
    }
  ],
  "max_recent_trials": 10
}
```

#### 场景记忆结构

`memory/scenario_memory.json`:

```json
{
  "holiday": {
    "accepted_patterns": [
      "holiday_position",
      "manual_public_holiday_features"
    ],
    "rejected_patterns": [
      "simple holiday binary only"
    ],
    "notes": "节假日前后偏差需要相对位置特征，不只是是否节假日",
    "typical_wape_range": [0.60, 0.75],
    "last_updated": "2026-06-17"
  },
  "cold_start": {
    "accepted_patterns": [
      "package_age_days",
      "recent_available_days"
    ],
    "notes": "新套餐需要供给可得性和生命周期特征",
    "last_updated": "2026-06-17"
  },
  "store_dish_day": {
    "warnings": [
      "package_detail 改善不代表 store_dish_day 一定改善"
    ],
    "required_checks": [
      "store_dish_day WAPE",
      "store_dish_day Bias"
    ],
    "last_updated": "2026-06-17"
  }
}
```

#### 记忆注入流程

```
T1/T2a Prompt 构造
        │
        ▼
┌─────────────────────────────┐
│ build_memory_context()      │
│                             │
│ 输入:                       │
│  long_term = memory/        │
│    long_term_memory.json    │
│  short_term = runs/         │
│    experiment_memory.json   │
│  scenario = memory/         │
│    scenario_memory.json     │
│  current_badcase_tags =     │
│    ["holiday", "store_dish"]│
│                             │
│ 输出 (注入到 prompt 头部):   │
│ ┌─────────────────────────┐ │
│ │ 长期硬规则:              │ │
│ │ - 禁止 eval label leakage│ │
│ │ - 禁止 pd.to_numeric... │ │
│ │                         │ │
│ │ 近期实验:                │ │
│ │ - trial_023 KEEP: ...   │ │
│ │ - trial_025 KEEP: ...   │ │
│ │                         │ │
│ │ 场景记忆 (holiday):      │ │
│ │ - 优先相对位置特征       │ │
│ │                         │ │
│ │ 最新人工指令:            │ │
│ │ - 保留 Tweedie...       │ │
│ └─────────────────────────┘ │
└─────────────────────────────┘
```

#### 记忆更新规则

| 触发条件 | 更新内容 | 更新位置 |
|---------|---------|---------|
| 人工 `/keep` | 写入 `accepted_patterns`，更新 `best_trial` | short_term + scenario |
| 人工 `/rollback` | 写入 `rejected_patterns`，回退到 `best_trial` | short_term + scenario |
| 人工 `/revise` | 写入 `latest_human_instruction` | short_term |
| 连续 2 次 KEEP | 晋升到 `stable_lessons`，待人工确认 | long_term |
| 严重代码失败 | 新增 `hard_rule`，需人工确认 | long_term |
| 每轮实验结束 | 追加 `recent_trials`，更新索引 | short_term + index |

---

### 2.5 实验记录体系

#### 每轮完整记录

`runs/trial_xxx/experiment_record.json`:

```json
{
  "trial_id": "trial_024",
  "parent_trial": "trial_023",
  "status": "revise",
  "decision_source": "human",

  "hypothesis": {
    "target_problem": "节假日偏高",
    "proposed_solution": "加入 holiday position + Tweedie"
  },

  "changes": [
    {
      "name": "holiday_position",
      "type": "add_feature",
      "files": ["agent2/code/src/lgb_package_to_dish_online_0319.py"],
      "summary": "新增节假日前后相对位置特征"
    }
  ],

  "metrics": {
    "primary": {
      "level": "package_detail",
      "old_wape": 0.7009,
      "new_wape": 0.6429,
      "wape_delta": 0.058
    },
    "secondary": {
      "level": "store_dish_day",
      "old_wape": 0.4908,
      "new_wape": 0.4910
    }
  },

  "human_feedback": {
    "decision": "revise",
    "feedback": "保留 Tweedie，去掉 group calibration"
  },

  "git": {
    "branch": "research/trial-024",
    "commit_sha": null,
    "pr_url": null
  },

  "artifacts": {
    "plan": "agent1/experiment_plan.yaml",
    "metrics": "evaluation/metric_comparison.json",
    "report": "final_report.md"
  }
}
```

#### 索引文件

`runs/experiment_index.jsonl` (一行一个 trial，支持快速 grep):

```jsonl
{"trial_id":"trial_023","status":"keep","wape":0.6429,"bias":0.0837,"parent":null,"changes":["tweedie","holiday_position"],"decision_source":"auto","timestamp":"2026-06-17T15:30:00+08:00"}
{"trial_id":"trial_025","status":"keep","wape":0.6206,"bias":0.0868,"parent":"trial_023","changes":["enhanced_activity","lifecycle_interaction"],"decision_source":"auto","timestamp":"2026-06-17T17:00:00+08:00"}
```

---

## 3. 当前已实现功能

### 3.1 功能矩阵

```
┌────────────────────────────────────────────────────────────┐
│                    已实现 ✅ / 规划中 ⏳                      │
├────────────────────────────┬──────────┬──────────┬─────────┤
│ 能力                       │ 单次模式 │ 循环模式 │ 链式继承│
├────────────────────────────┼──────────┼──────────┼─────────┤
│ T1 诊断 (6 skills)         │    ✅    │    ✅    │    ✅   │
│ T2a 计划 (3 skills)        │    ✅    │    ✅    │    ✅   │
│ T2b 代码生成 (1 skill)     │    ✅    │    ✅    │    ✅   │
│ T3 训练执行                │    ✅    │    ✅    │    ✅   │
│ T3 确定性 WAPE/Bias 计算   │    ✅    │    ✅    │    ✅   │
│ T3 keep/rollback 决策      │    ✅    │    ✅    │    ✅   │
│ T3 codegen retry ×2        │    ✅    │    ✅    │    ✅   │
│ T3 原始脚本兜底            │    ✅    │    ✅    │    ✅   │
│ T4 报告生成                │    ✅    │    ✅    │    ✅   │
│ 源码复制 + 数据复制        │    ✅    │    ✅    │    ✅   │
│ py_compile + 冒烟自验证    │    ✅    │    ✅    │    ✅   │
│ API Key / 设备码双认证     │    ✅    │    ✅    │    N/A  │
│ 额度检测+等待恢复+重试     │    ✅    │    ✅    │    N/A  │
│ 飞书通知 (单向)            │    ✅    │    ✅    │    N/A  │
│ 产物清单 WorkflowManifest  │    ✅    │    ✅    │    ✅   │
├────────────────────────────┼──────────┼──────────┼─────────┤
│ 飞书双向 Human-in-Loop     │    ⏳    │    ⏳    │    ⏳   │
│ Git 版本闭环               │    ⏳    │    ⏳    │    ⏳   │
│ 长期记忆注入               │    ⏳    │    ⏳    │    ⏳   │
│ 短期记忆 (实验历史)        │    ⏳    │    ⏳    │    ⏳   │
│ 场景记忆 (按业务分类)      │    ⏳    │    ⏳    │    ⏳   │
│ 实验完整记录+索引          │    ⏳    │    ⏳    │    ⏳   │
│ /branch 并行实验           │    ⏳    │    ⏳    │    ⏳   │
│ 场景记忆自动检索           │    ⏳    │    ⏳    │    ⏳   │
│ 长期规则自动晋升           │    ⏳    │    ⏳    │    ⏳   │
└────────────────────────────┴──────────┴──────────┴─────────┘
```

### 3.2 已实现的核心模块

| 模块 | 文件 | 行数 | 职责 |
|------|------|------|------|
| 编排引擎 | [codex_flow.py](codex_flow.py) | 1,420 | T1-T4 全流程 + 循环 + 链式 |
| 配置管理 | [config.py](config.py) | 114 | 统一配置加载 (JSON → dataclass) |
| 认证工具 | [codex_login.py](codex_login.py) | 80 | 设备码登录，session 持久化 |
| 飞书通知 | [lark_notify.py](lark_notify.py) | 260 | 单向通知 (start/trial/stop/error/credits) |
| 10 Skills | [skills/](skills/) | ~3,350 | 领域知识编码 (6 LLM + 4 混合) |

### 3.3 已验证的优化效果

| Trial | WAPE | Bias | 改善 | 决策 |
|-------|------|------|------|------|
| baseline | 0.7009 | +0.1695 | — | — |
| trial_023 | 0.6429 | +0.0837 | -8.3% | KEEP |
| trial_025 | 0.6206 | +0.0868 | -11.5% | KEEP |

---

## 4. 未来优化方向

### 4.1 路线图总览

```
Phase 1 ─────── Phase 2 ─────── Phase 3 ─────── Phase 4
记忆基础       飞书HIL        Git闭环        高级能力
(2周)          (3周)          (2周)           (3周)
───────        ───────        ───────         ───────
实验记录       飞书MCP        Git MCP         并行实验
JSON体系       双向交互       Server          场景检索
                              分支/PR         规则晋升
              反馈状态机      Commit规范      长期记忆
              消息协议        安全规则        自动晋级
                                              
记忆索引       超时策略       Diff摘要        归档清理
              指令解析       回退机制         Dashboard
```

### 4.2 Phase 1: 记忆基础 (约 2 周)

**目标**: 建立实验记录体系，让每轮实验有完整档案可追溯。

#### 交付物

| 任务 | 文件 | 说明 |
|------|------|------|
| 1.1 实验记录 | `runs/trial_xxx/experiment_record.json` | 每轮实验的完整档案 (假设/改动/指标/反馈/Git) |
| 1.2 短期记忆 | `runs/experiment_memory.json` | 最近 N 轮摘要 + best_trial + open_questions |
| 1.3 索引文件 | `runs/experiment_index.jsonl` | 行级索引，快速检索历史 trial |
| 1.4 长期记忆 | `memory/long_term_memory.json` | hard_rules + stable_lessons (初始版) |
| 1.5 场景记忆 | `memory/scenario_memory.json` | 按业务场景分类的经验 (初始版) |
| 1.6 记忆注入 | `build_memory_context()` | T1/T2a prompt 前自动构造记忆上下文 |

#### 验收标准

- [ ] 每轮实验自动生成 `experiment_record.json`
- [ ] `experiment_index.jsonl` 正确追加
- [ ] `experiment_memory.json` 每轮更新
- [ ] `hard_rules` 在每次 Codex prompt 中注入
- [ ] badcase 标签能匹配到对应场景记忆

---

### 4.3 Phase 2: 飞书 Human-in-Loop (约 3 周)

**目标**: 飞书从单向通知升级为双向决策入口。

#### 交付物

| 任务 | 文件 | 说明 |
|------|------|------|
| 2.1 Lark MCP Server | `mcp_servers/lark_research_server/server.py` | MCP Server 骨架 + 工具注册 |
| 2.2 飞书客户端 | `mcp_servers/lark_research_server/lark_client.py` | 消息发送/接收/解析 |
| 2.3 消息协议 | `mcp_servers/lark_research_server/schemas.py` | Review 卡片 / 反馈结构 / 状态机 |
| 2.4 send_experiment_review | 工具 | 发送 T3 评测结果 + 请求人工决策 |
| 2.5 wait_human_feedback | 工具 | 阻塞等待飞书回复 (含超时策略) |
| 2.6 反馈解析 | 工具 | 解析 `/keep /rollback /revise /branch /stop` |
| 2.7 状态机集成 | `codex_flow.py` 修改 | T3→飞书→人工决策→T4 闭环 |

#### 验收标准

- [ ] 飞书群收到 Review 卡片 (含指标/改动/风险)
- [ ] `/keep` 正确触发 Git commit + 记忆更新
- [ ] `/rollback` 正确回退 + 记录 rejected_patterns
- [ ] `/revise` 正确写入 latest_human_instruction
- [ ] 超时 24h 未响应自动 pause (可配置)
- [ ] 异常消息发送到飞书

---

### 4.4 Phase 3: Git 版本闭环 (约 2 周)

**目标**: 实验代码有版本追溯，KEEP 结果自动沉淀为 PR。

#### 交付物

| 任务 | 文件 | 说明 |
|------|------|------|
| 3.1 Git MCP Server | `mcp_servers/git_research_server/server.py` | MCP Server 骨架 + 安全规则 |
| 3.2 Git 客户端 | `mcp_servers/git_research_server/git_client.py` | 分支/提交/推送/PR 封装 |
| 3.3 create_trial_branch | 工具 | 实验前自动创建分支 |
| 3.4 diff_trial_changes | 工具 | 生成改动 diff 摘要发飞书 |
| 3.5 commit_trial_result | 工具 | KEEP 后提交 (含规范 Commit Message) |
| 3.6 create_pull_request | 工具 | 自动创建 PR (含指标摘要) |
| 3.7 rollback_to_parent | 工具 | ROLLBACK 后回退，不提交改动 |
| 3.8 集成 | `codex_flow.py` 修改 | T3 前后调用 Git MCP |

#### 验收标准

- [ ] 每轮实验自动创建 `research/trial-XXX` 分支
- [ ] diff 摘要随 Review 卡片一起发飞书
- [ ] `/keep` → commit + push + PR (人工二次确认)
- [ ] `/rollback` → 不提交，回退到父分支
- [ ] PR 描述含完整实验指标
- [ ] `reset --hard / force push / clean -fd` 被硬编码禁止

---

### 4.5 Phase 4: 高级能力 (约 3 周)

**目标**: 并行实验、自动检索、规则晋升、运维能力。

#### 交付物

| 任务 | 说明 |
|------|------|
| 4.1 /branch 并行实验 | 同一父 trial 分叉多个方向并行跑 |
| 4.2 场景记忆自动检索 | T1 badcase 标签自动匹配 scenario_memory |
| 4.3 长期规则自动晋升 | 连续 2 次 KEEP 自动候选晋升 stable_lessons |
| 4.4 额度 sleep + resume | 额度耗尽时不丢状态，恢复后自动继续 |
| 4.5 实验归档清理 | 超过 N 轮的旧实验自动压缩归档 |
| 4.6 Dashboard | 简要 Web 页面展示实验轨迹 (可选) |

#### 验收标准

- [ ] `/branch 方向A;方向B` 创建两个并行 trial
- [ ] badcase 标签 "holiday" 自动匹配 `scenario_memory["holiday"]`
- [ ] 某 pattern 连续 2 次 KEEP 时提示人工确认晋升
- [ ] 额度耗尽→自动 sleep→恢复后从断点继续
- [ ] 30 天前的 ROLLBACK trial 自动压缩

---

## 5. 实施排期

```
                     Week 1   Week 2   Week 3   Week 4   Week 5   Week 6   Week 7   Week 8   Week 9   Week 10
                     ──────   ──────   ──────   ──────   ──────   ──────   ──────   ──────   ──────   ──────
Phase 1: 记忆基础     ████████████████
  1.1 实验记录 JSON   ████████
  1.2 短期记忆        ████████
  1.3 索引文件              ████
  1.4 长期记忆              ████████
  1.5 场景记忆                    ████
  1.6 记忆注入                    ████████

Phase 2: 飞书 HIL              ████████████████████████
  2.1-2.3 MCP骨架                    ████████
  2.4-2.5 Review+反馈                      ████████
  2.6 指令解析                                   ████
  2.7 状态机集成                                  ████████

Phase 3: Git 闭环                                ████████  ████████
  3.1-3.2 MCP骨架                                        ████
  3.3-3.5 分支+提交+PR                                        ████
  3.6-3.8 PR+回退+集成                                         ████████

Phase 4: 高级能力                                                  ████████████████████████
  4.1 /branch 并行                                                      ████████
  4.2-4.3 场景检索+规则晋升                                                   ████████
  4.4-4.6 额度+归档+Dashboard                                                      ████████

────────────────────────────────────────────────────────────────────────────────────────────────────────
Milestones:                M1:记忆可用         M2:飞书闭环         M3:Git闭环          M4:全功能GA
                           (Week 2)            (Week 5)            (Week 7)            (Week 10)
```

### 里程碑说明

| 里程碑 | 时间 | 可交付能力 |
|--------|------|-----------|
| **M1: 记忆可用** | 第 2 周末 | 每轮实验有完整记录，长期规则在 prompt 中生效 |
| **M2: 飞书闭环** | 第 5 周末 | 飞书 Review → 人工 /keep /rollback /revise → 记忆更新 |
| **M3: Git 闭环** | 第 7 周末 | 实验分支 → diff → 人工确认 → commit + PR |
| **M4: 全功能 GA** | 第 10 周末 | 并行实验 + 自动检索 + 规则晋升 + 运维能力 |

---

## 6. 风险与应对

| 风险 | 概率 | 影响 | 应对措施 |
|------|------|------|---------|
| 飞书消息延迟/丢失 | 中 | 人工反馈超时 | timeout 策略 + 重试机制 + 降级为自动决策 |
| Git 冲突 (并行实验) | 中 | 代码合并困难 | 每分支隔离目录 + 冲突时人工介入 |
| 记忆膨胀 | 低 | prompt 过长 | recent_trials 上限 + 摘要压缩 |
| Codex SDK API 变更 | 低 | 主流程不可用 | 锁定 SDK 版本 + CI 检测兼容性 |
| 人工不响应 | 高 | 循环阻塞 | 超时 fallback 策略 (pause/auto-continue/stop) |
| 场景记忆误匹配 | 中 | 误导优化方向 | 人工审核晋升 + 置信度标注 + 手动回退 |

---

## 7. 附录

### 7.1 推荐最终目录结构

```
Codex_flow/
├── codex_flow.py                    # 主编排引擎
├── codex_login.py                   # 认证工具
├── config.py                        # 配置加载
├── lark_notify.py                   # 飞书通知 (将逐步被 Lark MCP 替代)
├── codex_flow_config.json           # 统一配置
├── requirements.txt                 # Python 依赖
│
├── mcp_servers/                     # MCP Server 层 (新增)
│   ├── lark_research_server/
│   │   ├── server.py                # Lark MCP Server 入口
│   │   ├── lark_client.py           # 飞书 API 封装
│   │   └── schemas.py               # 消息/反馈数据模型
│   └── git_research_server/
│       ├── server.py                # Git MCP Server 入口
│       ├── git_client.py            # Git 操作封装
│       └── schemas.py               # Git 操作数据模型
│
├── memory/                          # 记忆层 (新增)
│   ├── long_term_memory.json        # 长期记忆 (硬规则 + 稳定经验)
│   └── scenario_memory.json         # 场景记忆 (按业务分类)
│
├── skills/                          # 10 个 Skill (已有)
│   ├── using-forecast/
│   ├── forecast-task-planner/
│   ├── forecast-experiment-scanner/
│   ├── forecast-code-log-analyzer/
│   ├── forecast-evaluation-analyzer/
│   ├── forecast-badcase-locator/
│   ├── forecast-optimization-advisor/
│   ├── forecast-optimization-case-reference/
│   ├── forecast-trial-codegen/
│   └── forecast-report-writer/
│
├── baseline/                        # 基线实验目录 (已有)
│   ├── src/
│   ├── data/
│   └── outputs/
│
└── runs/                            # 实验产物 (增强)
    ├── experiment_memory.json       # 短期记忆 (新增)
    ├── experiment_index.jsonl       # 实验索引 (新增)
    ├── human_feedback_log.jsonl     # 反馈流水 (新增)
    └── trial_XXX/
        ├── experiment_record.json   # 实验完整档案 (新增)
        ├── human_feedback.json      # 人工反馈原文 (新增)
        ├── workflow_manifest.json   # 产物清单 (已有)
        ├── final_report.md          # 实验报告 (已有)
        ├── agent1/                  # 诊断+计划 (已有)
        ├── agent2/                  # 执行+代码 (已有)
        ├── evaluation/              # 指标对比 (已有)
        ├── standardized/            # 标准化数据 (已有)
        ├── audit/                   # 扫描分析 (已有)
        ├── reports/                 # 评测报告 (已有)
        ├── outputs/                 # 预测输出 (已有)
        └── logs/                    # 训练日志 (已有)
```

### 7.2 核心交互时序 (未来态)

```
codex_flow.py          Lark MCP           Git MCP          Memory Mgr         Human(飞书)
    │                     │                  │                 │                  │
    │─T3 完成────────────▶│                  │                 │                  │
    │                     │─发送Review卡片───│────────────────│─────────────────▶│
    │                     │                  │                 │                  │
    │                     │◀─────────────── /keep ──────────────────────────────│
    │                     │                  │                 │                  │
    │                     │─返回结构化反馈──▶│                 │                  │
    │                     │                  │                 │                  │
    │◀──decision:keep─────│                  │                 │                  │
    │                     │                  │                 │                  │
    │─写实验记录─────────────────────────────────────────────▶│                  │
    │                     │                  │                 │                  │
    │─更新记忆────────────────────────────────────────────────▶│                  │
    │                     │                  │                 │                  │
    │─commit+PR────────────────────────────▶│                 │                  │
    │                     │                  │                 │                  │
    │─T4 报告────────────▶│                  │                 │                  │
    │                     │─发送最终报告─────│────────────────│─────────────────▶│
```

### 7.3 配置项完整参考

```json
{
  "_comment": "Codex Flow v2.0 完整配置参考",

  "codex_home": "",
  "openai_api_key": "",
  "codex_api_key": "",
  "model": "gpt-5.5",

  "lark": {
    "enabled": true,
    "mcp_server": "lark_research",
    "chat_id": "oc_xxx",
    "review_timeout_hours": 24,
    "default_on_timeout": "pause",
    "commands": ["/keep", "/rollback", "/revise", "/branch", "/stop"]
  },

  "git": {
    "enabled": true,
    "mcp_server": "git_research",
    "repo_path": "baseline",
    "remote": "origin",
    "base_branch": "main",
    "trial_branch_prefix": "research/",
    "require_human_approval_for_push": true,
    "allow_force_push": false,
    "allow_reset_hard": false
  },

  "memory": {
    "long_term_path": "memory/long_term_memory.json",
    "scenario_path": "memory/scenario_memory.json",
    "short_term_path": "runs/experiment_memory.json",
    "max_recent_trials": 10,
    "auto_promote_kept_count": 2,
    "auto_promote_requires_manual_review": true
  },

  "experiment": {
    "record_path": "runs/trial_xxx/experiment_record.json",
    "index_path": "runs/experiment_index.jsonl",
    "feedback_log_path": "runs/human_feedback_log.jsonl"
  },

  "loop": {
    "max_iter": 10,
    "target_wape": null,
    "max_sleep_hours": 24.0,
    "human_review": true,
    "review_timeout": 86400
  }
}
```

---

> **一句话原则总结**  
> 长期记忆管"绝不能再犯的规则"  
> 短期记忆管"最近做过什么、结果如何"  
> 场景记忆管"不同业务场景下什么有效"  
> 飞书 MCP 管"人怎么决策"  
> Git MCP 管"代码怎么沉淀"  
> codex_flow.py 管"实验怎么跑"  
> Config 管"开关怎么控制"

---

*文档生成时间: 2026-06-18 | 作者: ComboScope Codex Flow Team*
