# ForeCast by Codex Flow

Codex Flow 是一个面向预测模型迭代的自动化实验系统。它把 Codex 的诊断、规划、代码生成和报告能力，与本地 Python 训练评估、飞书人工审核、阶段恢复、Git MCP 发布链路组合成一条可追踪、可恢复、可审计的多轮优化流程。

系统的核心原则是：大模型负责分析、设计实验、生成候选代码和撰写报告；训练、评估、KEEP/ROLLBACK/REVERSE 决策、checkpoint、路径隔离和 Git 发布由本地编排层明确执行。

## 快速开始

```powershell
# 1. 安装依赖
pip install -r requirements.txt

# 2. 登录 Codex，一次登录后 session 会持久化到 .codex_home
python codex_login.py --logout
python codex_login.py

# 3. 运行单次实验
python codex_flow.py `
  --experiment baseline `
  --ask "分析预测误差，提出特征实验并验证" `
  --output runs/trial_001

# 4. 运行多轮自动优化
python loop.py `
  --experiment baseline `
  --ask "持续优化预测模型，降低 WAPE" `
  --max-trials 10 `
  --review-timeout 1800
```

如需启用飞书通知和卡片审核，先设置环境变量：

```powershell
$env:FEISHU_APP_ID = "cli_xxx"
$env:FEISHU_APP_SECRET = "xxx"
$env:FEISHU_CHAT_ID = "oc_xxx"
$env:FEISHU_VERIFICATION_TOKEN = "xxx"
```

## 当前项目结构

```text
Codex_flow/
  baseline/                    # 当前有效模型、固定数据源和基线输出
    src/                       # 当前有效模型代码
    data/                      # 固定只读数据源，不复制到每个 trial
    outputs/                   # 基线预测输出
    requirements.txt

  runs/                        # 实验输出、状态、日志和全局产物
    <run>/
      trial_001/
        workflow_manifest.json # T1-T5 阶段状态和产物索引
        inputs/data_refs.json  # 固定数据源引用、大小、mtime、hash/schema 信息
        candidate/code/        # 当前 trial 候选代码
        standardized/          # 标准化数据和中间产物
        outputs/real_outputs/  # 当前 trial 训练/预测输出
        evaluation/            # 指标对比和自动决策
        reports/               # 中文实验报告
        logs/                  # 执行日志
        .checkpoint/           # 当前 trial checkpoint
    model_code_snapshots/      # KEEP/恢复相关模型快照
    feishu_card_actions.jsonl  # 飞书按钮/命令动作日志
    git_action_log.jsonl       # Git MCP 发布动作日志
    pr_drafts/                 # PR/MR 草稿产物

  skills/                      # Forecast 专用 Codex skills
  mcp_servers/                 # 本地 MCP server
    git_research_server/       # 受限 Git 发布工具，只允许模型代码边界
    lark_research_server/      # 飞书/Lark 相关工具适配

  codex_flow.py                # 单轮 T1-T5 工作流
  loop.py                      # 多轮实验状态机和人工审核编排
  checkpoint_manager.py        # checkpoint、快照、恢复和审计链
  config.py                    # flow_config.yaml + flow_paths.yaml 配置加载
  git_subagent.py              # Git subagent 入口
  lark_notify.py               # 飞书消息、命令、卡片和恢复通知
  lark_card_bot.py             # 飞书交互卡片回调服务
  card_server.py               # 卡片服务相关入口
  codex_login.py               # Codex 登录工具

  flow_config.yaml             # 行为配置
  flow_paths.yaml              # 路径契约
  requirements.txt             # Python 依赖
  PROJECT.md                   # 更完整的架构说明
  LARK_CARD_BOT.md             # 飞书卡片服务说明
```

## 工作流阶段

| 阶段 | 名称 | 执行方 | 说明 |
| --- | --- | --- | --- |
| T1 | Evaluate | Codex | 扫描实验目录，标准化输入，定位 badcase，提出优化方向。禁止训练和修改模型代码。 |
| T2a | Plan | Codex | 根据 T1 结果制定实验计划、特征假设和候选方案。 |
| Copy | Code Snapshot | Python | 将允许范围内的模型代码复制到 `candidate/code/`，数据和历史输出只做引用。 |
| T2b | Codegen | Codex | 生成候选训练代码，默认写入 `runs/<run>/trial_xxx/candidate/code/`。 |
| T3 | Execute | Python | 执行训练/预测/评估，计算指标并生成 KEEP、ROLLBACK、REVERSE 或 STOP 建议。 |
| T4 | Report | Codex | 生成中文实验报告。该阶段可降级，失败不会直接毁掉整轮实验。 |
| T5 | Feishu Card | Codex/Python | 生成并发送飞书审核卡片。该阶段可降级，支持后续人工恢复。 |

T3 是唯一真正执行训练和评估的阶段。日志中会明确出现 `[T3] 执行训练`，用于避免 T1/T2 阶段误跑长任务。

## 数据与代码隔离

项目采用“固定数据源 + trial 引用”的布局，避免每轮复制 4GB+ CSV：

```text
baseline/data/
  dish_package_feature_df.csv
  holiday_imformation.csv

runs/<run>/trial_xxx/
  inputs/data_refs.json
```

候选代码和有效模型代码严格隔离：

```text
baseline/src/                  # 当前有效模型代码
baseline/requirements.txt

runs/<run>/trial_xxx/
  candidate/code/              # 当前 trial 候选代码
  outputs/real_outputs/        # 当前 trial 输出
  evaluation/                  # 当前 trial 指标
  reports/                     # 当前 trial 报告
  logs/                        # 当前 trial 日志
```

KEEP 发布时只允许将下面路径应用回 `baseline/`：

```text
baseline/src/**
baseline/requirements.txt
```

数据、日志、训练输出和报告不会进入模型发布路径。

## 配置文件

`flow_config.yaml` 负责行为配置，例如 Codex、飞书、loop、恢复策略和 Git MCP：

```yaml
codex:
  home: ".codex_home"
  model: "gpt-5.5"

feishu:
  enabled: true
  poll_interval: 5

loop:
  max_iter: 10
  human_review:
    enabled: true
    timeout: 0
    auto_fallback: true

recovery:
  enabled: true
  codex_max_attempts: 2
  degradable_phases:
    - "report"
    - "feishu_card"

mcp:
  git:
    enabled: true
    baseline_dir: "baseline"
    trial_code_subdir: "candidate/code"
    allowed_paths:
      - "baseline/src/**"
      - "baseline/requirements.txt"
```

`flow_paths.yaml` 负责路径契约，例如固定数据源、trial 子目录、全局日志和快照目录。需要本机覆盖时可创建 `flow_paths.local.yaml`，该文件不应提交。

敏感信息建议通过环境变量注入：

| 环境变量 | 用途 |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI API Key |
| `CODEX_API_KEY` | Codex API Key |
| `CODEX_MODEL` | 覆盖 Codex 模型 |
| `CODEX_HOME` | 覆盖 Codex home |
| `FEISHU_APP_ID` | 飞书应用 ID |
| `FEISHU_APP_SECRET` | 飞书应用密钥 |
| `FEISHU_CHAT_ID` | 飞书目标群聊 |
| `FEISHU_VERIFICATION_TOKEN` | 飞书卡片回调校验 token |

## 常用命令

### 单次实验

```powershell
python codex_flow.py `
  --experiment baseline `
  --ask "分析预测误差并提出优化方案" `
  --output runs/trial_001
```

### 链式实验

```powershell
python codex_flow.py `
  --experiment baseline `
  --ask "基于上一轮结果继续优化" `
  --output runs/trial_002 `
  --previous-trial runs/trial_001
```

### 多轮实验

```powershell
python loop.py `
  --experiment baseline `
  --ask "持续优化预测模型" `
  --output runs `
  --max-trials 10 `
  --target-wape 0.55
```

### 关闭人工审核

```powershell
python loop.py `
  --experiment baseline `
  --ask "全自动优化预测模型" `
  --max-trials 5 `
  --no-review
```

### 启动飞书卡片回调服务

```powershell
python lark_card_bot.py --host 0.0.0.0 --port 8787
```

飞书开发者后台的卡片回调地址配置为：

```text
https://<public-host>/feishu/card
```

更多细节见 `LARK_CARD_BOT.md`。

## 人工审核与恢复命令

飞书审核支持文本命令和交互卡片按钮。动作会写入：

```text
runs/feishu_card_actions.jsonl
```

| 命令 | 效果 |
| --- | --- |
| `/keep` | 接受当前 trial，作为新的有效模型 |
| `/rollback` | 拒绝当前 trial，同参数重试或回到上一个有效模型 |
| `/reverse` | 放弃当前方向，回到历史 keep 点 |
| `/stop` | 停止 loop |
| `/revise <补充说明>` | 带补充意见继续下一轮 |
| `/branch A;B` | 从历史版本分支出新的实验方向 |
| `/status` | 查询当前 loop 状态 |
| `/resume` | 从中断阶段继续 |
| `/retry-stage <phase>` | 重试指定阶段 |
| `/skip-stage <phase>` | 跳过可降级阶段 |

## Git MCP 发布边界

KEEP 后可通过 `git_subagent.py` 和 `mcp_servers/git_research_server/` 发布模型代码。Git 工具被限制在模型发布边界内：

```text
baseline/src/**
baseline/requirements.txt
```

非 KEEP 决策不会推送远程仓库。当前配置默认面向 `forecastops/ForecastModel` 远端分支，`create_pr_on_keep` 默认关闭，避免误用 GitHub PR 逻辑操作 GitLab 仓库。

Git 动作日志写入：

```text
runs/git_action_log.jsonl
```

## 测试

当前测试覆盖路径配置、trial 代码布局、loop dry-run、阶段恢复、Git 发布和 MCP 适配：

```powershell
python -m unittest `
  test_path_config.py `
  test_trial_code_layout.py `
  test_agent_loop_dryrun.py `
  test_mcp_adapters.py `
  test_stage_recovery_flow.py `
  test_git_publish_flow.py
```

也可以运行单个测试文件，例如：

```powershell
python -m unittest test_path_config.py
```

## 相关文档

- `PROJECT.md`：更完整的架构、状态机、恢复机制和发布链路说明。
- `LARK_CARD_BOT.md`：飞书卡片回调服务配置和本地测试说明。
- `REPORT.md` / `SUMMARY.md` / `TECHNICAL_PROPOSAL.md`：历史报告、总结和技术方案材料。
