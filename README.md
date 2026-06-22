# ForeCast by Codex Flow

基于 OpenAI Codex SDK 的预测实验自动诊断、优化与多轮实验工作流。

输入预测实验目录 + 优化目标 → 自动完成：**诊断 → 计划 → 代码生成 → 训练验证 → 报告 → 飞书人工审核**。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 登录 (一次性, session 持久化)
python codex_login.py

# 3. 单次实验
python codex_flow.py \
  --experiment baseline \
  --ask "分析预测误差，提出特征实验并验证" \
  --output runs/trial_001

# 4. 多轮自动优化循环 (带飞书人工审核)
export FEISHU_APP_ID="cli_xxx"
export FEISHU_APP_SECRET="xxx"
python loop.py \
  --experiment baseline \
  --ask "分析预测误差并持续优化" \
  --max-trials 10 \
  --review-timeout 1800
```

## 架构

```
codex_flow.py           loop.py
    │                      │
    │  T1~T5 单次流水线      │  多轮实验状态机
    │                      │  keep / reverse / rollback
    │                      │  + 飞书人工审核
    ▼                      ▼
  ┌──────────────────────────────────────┐
  │        Codex Session                  │
  │  ┌──────┬──────┬──────┬──────┬────┐  │
  │  │  T1  │ T2a  │ T2b  │ T4   │ T5 │  │
  │  │ 诊断 │ 计划 │ 代码 │ 报告 │ 卡片│  │
  │  └──────┴──────┴──────┴──────┴────┘  │
  │            T3: Python 确定性执行        │
  └──────────────────────────────────────┘
         │                    │
         ▼                    ▼
    skills/  (10 SKILL.md)   lark_notify.py
                             飞书消息 + 卡片审核
```

### 流水线阶段

| 阶段 | 执行方 | 说明 |
|------|--------|------|
| **T1 Evaluate** | Codex LLM | 扫描实验目录 → 标准化 → badcase 定位 → 优化建议 |
| **T2a Plan** | Codex LLM | 特征假设 + 实验计划 + 候选实验方案 |
| **Copy** | Python | 源码复制到 agent2/code/ (链式模式继承上一轮) |
| **T2b Codegen** | Codex LLM | 代码生成 + 自验证 (py_compile + 冒烟测试) |
| **T3 Execute** | Python | 确定性训练 → 指标计算 → keep/rollback 决策 |
| **T4 Report** | Codex LLM | 中文实验验证报告 |
| **T5 Card** | Codex LLM | 飞书审核卡片 (≤40 行, 非致命) |

### 多轮循环 (loop.py)

```
Round N 完成 ──→ 自动决策 (来自 T3 指标对比)
    │
    ├─ keep    → 接受作为新 baseline, 将人工补充注入下轮 Ask
    ├─ reverse → 放弃当前方向, 恢复到历史 keep 点, 排除失败方向
    ├─ rollback→ 同参数重试 (最多 3 次/轮)
    └─ stop    → 结束循环

人工审核 (可选, 需飞书配置):
  文字命令: /keep /reverse /rollback /stop /revise <文本> /branch A;B
  卡片按钮: KEEP / ROLLBACK / REVISE (通过 lark_card_bot.py 回调)
```

安全护栏：连续 2 次 reverse → 强制停止 | 单轮 3 次 rollback → 强制 reverse | WAPE 改善 < 0.005 连续 2 轮 → 停止

## 项目结构

```
codex_flow/                     # 项目根目录
├── codex_flow.py               # 核心引擎: T1~T5 流水线 + 单次/循环 CLI
├── loop.py                     # 多轮实验循环 (状态机 + 人工审核 + 断点续跑)
├── codex_login.py              # 独立登录工具 (设备码认证)
├── config.py                   # 配置系统 (YAML + 环境变量)
├── checkpoint_manager.py       # 状态持久化 (快照/恢复/追溯链/收敛检测)
├── lark_notify.py              # 飞书 HTTP 客户端 (消息收发 + 命令解析 + 审核阻塞)
├── lark_card_bot.py            # 飞书卡片回调 HTTP 服务器 (端口 8787)
├── flow_config.yaml            # 主配置文件
├── requirements.txt            # Python 依赖
├── README.md                   # 本文件
│
├── skills/                     # 10 个 forecast 专用技能
│   ├── using-forecast/              # 流程调度器
│   ├── forecast-task-planner/       # 任务意图解析
│   ├── forecast-experiment-scanner/ # 实验目录扫描
│   ├── forecast-code-log-analyzer/  # 代码/日志分析
│   ├── forecast-evaluation-analyzer/# 指标计算 + 场景标签 + 异常检测
│   ├── forecast-badcase-locator/    # badcase 挖掘
│   ├── forecast-optimization-advisor/    # 优化建议生成
│   ├── forecast-optimization-case-reference/ # 案例模式库
│   ├── forecast-trial-codegen/      # 代码生成 (14 条硬约束)
│   ├── forecast-report-writer/      # 中文评测报告
│   └── forecast-feishu-review-card/ # 飞书审核卡片生成
│
└── runs/                        # 输出目录
    ├── trial_NNN/               # 单次实验产出
    │   ├── workflow_manifest.json     # 流水线状态
    │   ├── audit/                     # T1 扫描结果
    │   ├── agent1/                    # T1/T2a 分析产物
    │   ├── agent2/                    # T2b 代码生成产物
    │   ├── standardized/              # 标准化数据
    │   ├── evaluation/                # T3 指标对比
    │   ├── reports/                   # T4 报告
    │   ├── feishu_review_card.md      # T5 飞书卡片
    │   └── .checkpoint/               # 代码快照 (用于回溯)
    └── .loop_state.json          # 循环追溯状态
```

## 配置

配置文件 `flow_config.yaml`：

```yaml
codex:
  model: "gpt-5.5"         # 默认模型

auth:
  openai_api_key: ""        # 或设置环境变量 OPENAI_API_KEY
  codex_api_key: ""

feishu:
  enabled: true
  app_id: "cli_xxx"
  app_secret: ""            # 建议用环境变量 FEISHU_APP_SECRET
  chat_id: "oc_xxx"        # 目标群聊 ID
  verification_token: ""    # 卡片回调验证
  poll_interval: 5          # 消息轮询间隔 (秒)

loop:
  max_iter: 10              # 最大实验轮次
  target_wape: null         # 目标 WAPE, 达成即停止
  max_sleep_hours: 24.0     # 配额恢复最长等待
  human_review:
    enabled: true           # 启用飞书人工审核
    timeout: 1800           # 审核超时 (秒)
    auto_fallback: true     # 超时后使用自动决策
    authorized_senders: []  # 限制命令作者 (空=不限制)
  limits:
    max_consecutive_reverses: 2
    max_rollbacks_per_round: 3
  convergence:
    min_wape_improvement: 0.005
    max_rounds_without_improvement: 2

paths:
  experiment_dir: "baseline"
  runs_dir: "runs"
  skills_dir: "skills"
```

敏感字段支持环境变量覆盖：

| 环境变量 | 对应配置 |
|----------|----------|
| `OPENAI_API_KEY` | auth.openai_api_key |
| `CODEX_API_KEY` | auth.codex_api_key |
| `CODEX_MODEL` | codex.model |
| `CODEX_HOME` | codex.home |
| `FEISHU_APP_ID` | feishu.app_id |
| `FEISHU_APP_SECRET` | feishu.app_secret |
| `FEISHU_CHAT_ID` | feishu.chat_id |
| `FEISHU_VERIFICATION_TOKEN` | feishu.verification_token |

## 认证

三种认证方式，优先级递减：

1. **API Key** — 设置 `OPENAI_API_KEY` 环境变量或 `flow_config.yaml` 中的 `auth.openai_api_key`
2. **持久化 Session** — 运行 `python codex_login.py` 完成设备码登录, session 自动写入 `~/.codex/sessions/`
3. **现场设备码登录** — 无 API Key 且无持久化 session 时, 自动触发

Session 验证使用实际模型调用 (`thread_start()` + `run("OK")`), 不依赖 `account()` API。

## 飞书集成

### 工作原理

```
每轮实验完成
    │
    ├── 飞书群收到指标摘要 (markdown)
    ├── 飞书群收到交互卡片 (含 KEEP/ROLLBACK/REVISE 按钮)
    │
人工操作:
    ├── 文字回复 /keep 或 /rollback 或 /revise 建议
    └── 或点击卡片按钮
        └── Feishu POST → lark_card_bot.py (:8787) → 写入 JSONL
    │
    └── loop.py 收到决策 → 继续下一轮
```

### 启动卡片回调服务

```bash
# 需要公网可达或内网穿透
python lark_card_bot.py --port 8787
```

卡片按钮回调走 HTTP 服务器; 文字命令走 IM 消息轮询。两者并行, 任一先到即生效。

## 使用示例

### 单次实验

```bash
python codex_flow.py \
  --experiment baseline \
  --ask "分析预测误差并提出优化方案" \
  --output runs/trial_001
```

### 链式实验 (继承上一轮代码)

```bash
python codex_flow.py \
  --experiment baseline \
  --ask "基于 trial_001 的分析结果继续优化" \
  --output runs/trial_002 \
  --previous-trial runs/trial_001
```

### 多轮自动循环

```bash
python codex_flow.py \
  --experiment baseline \
  --ask "持续优化预测模型" \
  --loop --max-iter 10 --target-wape 0.55
```

### 多轮循环 + 飞书人工审核

```bash
python loop.py \
  --experiment baseline \
  --ask "持续优化预测模型，每轮等待人工审核" \
  --max-trials 10 \
  --review-timeout 1800
```

### 实验目录要求

```
experiment/
├── src/           # 训练源码
├── data/          # 训练数据
└── outputs/       # 基线预测输出 (含 split 列 + pred/true 列)
```

### 飞书审核命令

| 命令 | 效果 |
|------|------|
| `/keep` | 接受本轮, 作为新 baseline |
| `/rollback` | 拒绝, 同参数重试 |
| `/reverse` | 放弃方向, 回到历史 keep 点 |
| `/stop` | 结束实验循环 |
| `/revise <补充>` | keep + 将补充文本注入下轮 Ask |
| `/branch A;B` | 从历史版本分支新实验 |
| `/status` | 查询当前循环状态 |
