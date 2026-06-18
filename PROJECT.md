# ComboScope Codex Flow — 预测实验自动诊断与优化工作流

## 1. 项目概述

基于 OpenAI Codex SDK 的轻量级预测实验自动化工作流。输入一个预测实验目录和优化目标，自动完成：**诊断 → 计划 → 代码生成 → 训练验证 → 报告**。

**核心理念**：Codex SDK 负责"想清楚做什么"（LLM 推理），Python 确定性脚本负责"精确执行 + 判断结果"。786 行代码替代原 ComboScope ~5000 行 LangGraph 编排。

```
输入: baseline 预测实验目录 + "分析预测误差，提出特征实验并验证"
输出: final_report.md + WAPE 对比 + keep/rollback 决策
```

---

## 2. 整体流程结构

```
┌──────────────────────────────────────────────────────────┐
│              codex_flow.py 编排流程                        │
│         1 个 Codex Session → 4 LLM Threads + 3 Python 函数 │
└──────────────────────────────────────────────────────────┘

  CLI: python codex_flow.py --experiment baseline --ask "..." --output runs/trial_xxx
  │
  ▼
┌─ Python: _ensure_dirs() ────────────────────────────────┐
│  创建 agent1/ agent2/code/ evaluation/ reports/ ...      │
└──────────────────────────────────────────────────────────┘
  │
  ▼
╔══════════════════════════════════════════════════════════╗
║           Codex Session (认证 + 工作同一 session)          ║
║                                                          ║
║  ╔══ Codex SDK (LLM 驱动) ════════════════════════════╗  ║
║  ║                                                     ║  ║
║  ║  T1: Evaluate + Diagnose          [~4 min, ~56K tok] ║  ║
║  ║  Skills: using-forecast, task-planner, scanner,     ║  ║
║  ║          code-log-analyzer, badcase-locator,         ║  ║
║  ║          optimization-advisor                        ║  ║
║  ║  产出: audit/ agent1/ standardized/ reports/         ║  ║
║  ║  职责: 扫描目录 → 分析代码日志 → 定位badcase          ║  ║
║  ║        → 生成优化建议 → 标准化数据                    ║  ║
║  ║        (不计算指标, 指标由 T3 确定性负责)             ║  ║
║  ║                                                     ║  ║
║  ║  T2a: Plan                       [~3 min, ~37K tok] ║  ║
║  ║  Skills: optimization-advisor, case-reference,       ║  ║
║  ║          task-planner                                ║  ║
║  ║  产出: agent1/experiment_plan.yaml                   ║  ║
║  ║         agent1/feature_hypothesis.yaml               ║  ║
║  ║         reports/forecast_report.md                   ║  ║
║  ║  职责: 基于证据 → 特征假设 → 可执行实验计划          ║  ║
║  ║                                                     ║  ║
║  ╚═════════════════════════════════════════════════════╝  ║
║                                                          ║
║  ┌─ Python: copy_source_to_trial() ──────────────────┐   ║
║  │  baseline/*.py + data/*.csv → agent2/code/         │   ║
║  └────────────────────────────────────────────────────┘   ║
║                                                          ║
║  ╔══ Codex SDK (LLM 驱动) ════════════════════════════╗  ║
║  ║                                                     ║  ║
║  ║  T2b: Code Generation             [~4 min, ~42K tok] ║  ║
║  ║  Skills: forecast-trial-codegen                     ║  ║
║  ║  产出: agent2/code/train.py                         ║  ║
║  ║         agent2/agent2_execution_plan.yaml           ║  ║
║  ║  自验证: py_compile + 冒烟测试 → 不通过不输出       ║  ║
║  ║  职责: 按 experiment_plan 生成特征修改代码           ║  ║
║  ║                                                     ║  ║
║  ╚═════════════════════════════════════════════════════╝  ║
║                                                          ║
║  ┌─ Python: execute_t3() ─────────────────────────────┐   ║
║  │                                                     │   ║
║  │  ① subprocess: python train.py → 训练               │   ║
║  │  ② 读取 output CSV → test 集 WAPE/Bias 计算         │   ║
║  │  ③ 读取 baseline/outputs/ → baseline test 指标      │   ║
║  │  ④ 确定性决策: keep ⇔ WAPE↓≥0.005 & |Bias|↑<0.02    │   ║
║  │  ⑤ 失败回退: thread_resume → codegen 修复 (×2)      │   ║
║  │               → 终极回退: 原始脚本 baseline preset   │   ║
║  │                                                     │   ║
║  └─────────────────────────────────────────────────────┘   ║
║                                                          ║
║  ╔══ Codex SDK (LLM 驱动) ════════════════════════════╗  ║
║  ║                                                     ║  ║
║  ║  T4: Report                       [~2 min]          ║  ║
║  ║  Skills: forecast-report-writer                     ║  ║
║  ║  产出: final_report.md                              ║  ║
║  ║  职责: 基于全部产物撰写实验验证结论报告              ║  ║
║  ║                                                     ║  ║
║  ╚═════════════════════════════════════════════════════╝  ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝
  │
  ▼
┌─ Python: WorkflowManifest ───────────────────────────────┐
│  workflow_manifest.json (各阶段 thread_id, artifacts, err) │
└──────────────────────────────────────────────────────────┘
```

### 职责边界

| | Codex SDK (LLM) | Python 确定性 |
|---|---|---|
| **阶段** | T1 诊断、T2a 计划、T2b 代码生成、T4 报告 | 目录初始化、文件复制、T3 训练+评测+决策 |
| **特点** | LLM 推理，非确定性 | 确定性规则，可复现 |
| **Token 消耗** | ~135K tokens/run | 0 |
| **耗时** | ~13 分钟 | 取决于训练脚本 (~1h) |
| **负责** | "想清楚做什么" | "精确执行 + 判断结果" |

---

## 3. 最新运行结果 (trial_023)

### 3.1 实验信息

| 项目 | 内容 |
|------|------|
| Trial ID | trial_023 |
| 实验目录 | `baseline/` |
| 模型 | LightGBM |
| 原始 objective | regression |
| 优化策略 | Tweedie objective + T+2 availability + holiday position + package activity + group calibration + recent ratio |

### 3.2 codegen 修改策略

`train.py` 采用**薄封装 + 参数覆盖**模式，不改核心源码逻辑：

```python
def apply_trial_023_changes(args):
    args.experiment = "exp_02_tweedie"
    args = apply_experiment_preset(args)
    args.objective = "tweedie"
    args.tweedie_variance_power = 1.2
    args.ratio_strategy = "recent"
    args.enable_holiday_position = True
    args.enable_t2_availability_features = True
    args.enable_manual_public_holiday_features = True
    args.enable_package_activity_features = True
    args.enable_group_calibration = True
    return args
```

源码修改：新增 `numeric_column_or_default()` helper，修复了 `pd.to_numeric(df.get(col, 0)).fillna()` 反模式（8 处），消除了此前 6 次训练失败的根因。

### 3.3 指标对比

#### 主指标：package_detail (模型输出级, test 集 57,406 行)

| 指标 | baseline | trial_023 | 变化 | 
|------|----------|-----------|------|
| **WAPE** | 0.7009 | **0.6429** | **-8.3%** ✅ |
| **Bias** | +0.1695 | **+0.0837** | **-50.6%** ✅ |

#### 辅助指标：store_dish_day (dish 分配后, test 集 472,551 行)

| 指标 | baseline |
|------|----------|
| WAPE | 0.4908 |
| Bias | +0.0781 |

### 3.4 决策

```
Decision: KEEP
Reason:   package_detail WAPE delta=+0.0580, Bias delta=-0.0858
          训练成功, 评测成功
```

满足 keep 条件：
- ✅ WAPE 改善 0.0580 > 0.005
- ✅ |Bias| 变化 -0.0858 < 0.02
- ✅ 训练成功 (rc=0)
- ✅ 评测成功

---

## 4. 费用分析

### 4.1 Token 消耗明细 (单次实验)

| 阶段 | 输入 Tokens | 输出 Tokens | 说明 |
|------|------------|------------|------|
| T1 evaluate | ~56,000 | ~340 | 7 个 skills + 目录扫描 |
| T2a plan | ~37,000 | ~240 | 3 个 skills + 计划生成 |
| T2b codegen | ~42,000 | ~310 | 1 个 skill + 代码生成+验证 |
| T4 report | ~5,000 | ~100 | 1 个 skill + 报告撰写 |
| **合计** | **~140,000** | **~1,000** | |
| codegen retry (平均 1 次) | ~10,000 | ~200 | 仅在首次训练失败时触发 |

### 4.2 GPT-5 费用估算

| 项目 | 用量 | 单价 | 费用 |
|------|------|------|------|
| 输入 | ~140,000 tokens | $27.50/1M tokens | **$3.85** |
| 输出 | ~1,000 tokens | $137.50/1M tokens | **$0.14** |
| **单次实验总价** | | | **~$3.99** |

加上 codegen retry：**~$4.08/次**。

### 4.3 与人工对比

| 方式 | 耗时 | 费用 |
|------|------|------|
| Codex 自动化 | ~1.5h (含训练) | ~$4 |
| 人工数据科学家 | 2-4h | $100-200 |

---

## 5. 从单次执行扩展到多次迭代优化

### 5.1 目标

在上一轮有效实验的基础上继续优化，形成 **"诊断 → 实验 → 验证 → 再诊断" 的自动研究循环**。

### 5.2 架构设计

```
┌─────────────────────────────────────────────────────────┐
│                    Loop 编排层                           │
│  python loop.py --experiment baseline --ask "..."        │
│       --max-trials 5 --improvement-threshold 0.005       │
└─────────────────────────────────────────────────────────┘
  │
  ▼
  trial_001: baseline 诊断 → 选最高优先级 change → 训练 → 评估
  │            ┌── KEEP: 产物作为下一轮 baseline
  │            └── ROLLBACK: 试下一个 candidate_experiment
  ▼
  trial_002: 基于 trial_001 的优化产物 → 新诊断 → 新 change → 训练
  │            (只分析 trial_001 引入的新误差模式)
  ▼
  trial_003: ...
  │
  ▼
  trial_N: WAPE 改善 < 阈值 或 达到最大轮次 → 输出最优方案
```

### 5.3 关键实现要点

#### 5.3.1 产物传递链

```
trial_N/agent1/experiment_plan.yaml
    │  changes 中标记 status: kept/rolled_back
    ▼
trial_N+1/
    │  T1 读取上一轮 kept changes 作为已知上下文
    │  T2a 排除已验证无效的方向
    │  T2b 在 kept 代码基础上增量修改 (而非从头生成)
    ▼
trial_N+1/agent2/code/  ← 从 trial_N/ 的 kept 版本 fork
```

#### 5.3.2 `loop.py` 伪代码

```python
def run_loop(experiment_dir, ask, max_trials=5):
    kept_changes = []
    best_wape = None
    current_baseline = experiment_dir  # 第一轮用原始 baseline

    for i in range(1, max_trials + 1):
        trial_id = f"trial_{i:03d}"
        
        # 为当前 trial 准备上下文
        context = {
            "previous_kept_changes": kept_changes,
            "baseline_wape": best_wape,
            "trial_id": trial_id,
        }
        
        # 单次执行
        manifest = run_workflow(
            experiment_dir=current_baseline,
            ask=augment_ask(ask, context),  # 附加历史上下文
            output_dir=f"runs/{trial_id}",
        )
        
        # 读取决策
        comparison = read_comparison(f"runs/{trial_id}")
        
        if comparison["decision"] == "keep":
            kept_changes.append({
                "trial": trial_id,
                "changes": read_experiment_plan(f"runs/{trial_id}"),
                "wape_delta": comparison["wape_delta"],
            })
            best_wape = comparison["primary"]["new_wape"]
            # 下一轮 baseline 指向当前产物
            current_baseline = f"runs/{trial_id}"
        else:
            # rollback: 排除失败方向
            failed_changes = read_experiment_plan(f"runs/{trial_id}")
            context["excluded_directions"].append(failed_changes)
        
        # 收敛判断
        if comparison["wape_delta"] < 0.005:
            print(f"WAPE 改善不足, 停止迭代")
            break
    
    # 选择最优方案
    best_trial = select_best(kept_changes)
    generate_final_report(best_trial, kept_changes)
```

#### 5.3.3 `augment_ask` — 上下文增强

```python
def augment_ask(original_ask, context):
    return f"""
{original_ask}

历史实验上下文:
- 已验证有效的修改: {context['previous_kept_changes']}
- 当前 baseline WAPE: {context['baseline_wape']}
- 排除方向: {context.get('excluded_directions', [])}

注意: 不要重复已验证有效或已排除的修改方向。
      基于当前 baseline 的剩余误差模式提出新方向。
"""
```

#### 5.3.4 产物继承策略

```
trial_001/agent2/code/train.py  (KEEP)
    │
    ▼  copy to
trial_002/agent2/code/train.py  ← codegen 基于此增量修改
    │  只追加新 change, 保留已验证的 change
    │
    ▼  (KEEP)
trial_003/agent2/code/train.py  ← 继续增量...
```

### 5.4 收敛策略

| 条件 | 动作 |
|------|------|
| WAPE delta < 0.005 | 连续 2 轮改善不足 → 停止 |
| candidate_experiments 耗尽 | 所有候选方案已尝试 → 停止 |
| 达到 max_trials | 强制停止 → 选最优 |
| 出现 KEEP | 产物继承 → 继续下一轮 |
| 出现 ROLLBACK | 排除方向 → 试下一个候选 |

### 5.5 最优方案选择

```python
def select_best(kept_changes):
    """在所有 KEEP 的 trial 中选择 WAPE 最低的"""
    return min(kept_changes, key=lambda t: t["wape"])
```

---

## 6. 文件结构

```
Codex_flow/
├── codex_flow.py              # 主工作流 (单次执行, ~870 行)
├── loop.py                    # 多次迭代编排 (待实现)
├── baseline/                  # 预测实验目录
│   ├── src/                   # 训练源码
│   ├── data/                  # 训练数据 (4.6GB CSV)
│   ├── outputs/               # 基线预测输出 (含 split 列)
│   └── requirements.txt
├── forecastops-agent-master/  # Skills 定义 (10 个 skill)
│   └── skills/
│       ├── using-forecast/           # 流程调度器
│       ├── forecast-task-planner/    # ask → 任务意图
│       ├── forecast-experiment-scanner/ # 目录扫描
│       ├── forecast-code-log-analyzer/  # 代码/日志分析
│       ├── forecast-evaluation-analyzer/ # 指标计算脚本
│       ├── forecast-badcase-locator/     # badcase 挖掘
│       ├── forecast-optimization-advisor/ # 优化建议
│       ├── forecast-optimization-case-reference/ # 案例参考
│       ├── forecast-trial-codegen/     # 代码生成规则 (14 条)
│       └── forecast-report-writer/     # 报告撰写
├── runs/                      # 实验输出
│   └── trial_023/
│       ├── workflow_manifest.json
│       ├── final_report.md
│       ├── agent1/            # 诊断 + 计划
│       ├── agent2/            # 执行计划 + 代码 + 审查
│       ├── evaluation/        # 指标对比
│       ├── standardized/      # 标准化数据
│       ├── audit/             # 扫描 + 代码/日志分析
│       ├── reports/           # 评测报告 + 优化建议
│       └── logs/              # train.log
└── PROJECT.md                 # 本文档
```

---

## 7. 使用方式

### 7.1 单次执行

```bash
# 设备码登录
python codex_flow.py \
  --experiment baseline \
  --ask "分析预测误差，提出特征实验并验证" \
  --output runs/trial_001

# API Key 登录 (无过期问题)
export OPENAI_API_KEY="sk-..."
python codex_flow.py \
  --experiment baseline \
  --ask "分析预测误差，提出特征实验并验证" \
  --output runs/trial_001
```

### 7.2 多次迭代执行 (规划中)

```bash
python loop.py \
  --experiment baseline \
  --ask "分析预测误差，提出特征实验并验证" \
  --max-trials 5 \
  --improvement-threshold 0.005
```

### 7.3 手动续跑 (token 过期时)

```bash
# 修改 codex_flow.py 中的 run_workflow, 跳过已完成的阶段
# 或使用 thread_resume 继续:
python -c "
from openai_codex import Codex
from codex_flow import CODEX_CONFIG, THREAD_REPORT
with Codex(config=CODEX_CONFIG) as c:
    t = c.thread_start(cwd='runs/trial_023')
    prompt = THREAD_REPORT.prompt.format(...)
    t.run(prompt)
"
```

---

## 8. 已知限制与改进方向

| 限制 | 改进方向 |
|------|---------|
| 仅支持 OpenAI 模型 | 抽象 LLM 层支持 Anthropic/DeepSeek/GLM |
| 单次 session token 过期 (~1h) | API Key 模式或 session 内 re-auth |
| codegen 不带状态跨 trial | loop.py 实现产物继承 |
| 训练脚本内网路径依赖 | 全部改为 CLI 参数控制 |
| WAPE 仅 test 集 | 已修复 (T3 确定性读取 outputs/) |
