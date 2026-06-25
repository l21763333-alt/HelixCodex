# ForeCast by Codex Flow

Codex Flow 是一个面向预测模型迭代的自动化实验系统。它把 Codex 的诊断、规划、代码生成和报告能力，与本地 Python 训练评估、飞书人工审核、阶段恢复、Git MCP 发布链路组合成一条可追踪、可恢复、可审计的多轮优化流程。

系统的核心原则是：大模型负责分析、设计实验、生成候选代码和撰写报告；训练、评估、KEEP/ROLLBACK/REVERSE 决策、checkpoint、路径隔离和 Git 发布由本地编排层明确执行。

## 冷启动配置清单

新机器从零启动时，建议先按下面顺序完成环境、配置、登录和最小化验证。

### 1. 创建并启用虚拟环境

Linux 服务器：

```bash
cd /srv/codex/Codex_flow
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

模型训练依赖由模型仓库自己的 `baseline/requirements.txt` 管理。若要真实执行 T3 训练，还需要安装模型依赖：

```bash
pip install -r ../ForecastModel_worktree/baseline/requirements.txt
```

当前模型依赖通常包括 `pandas`、`numpy`、`scikit-learn`、`lightgbm`、`pyodps`。如果 Git MCP worktree 尚未准备好，也可先用本项目内的 `baseline/requirements.txt`。

### 2. 配置 `flow_config.yaml`

`flow_config.yaml` 放运行行为，不放密钥。冷启动时最需要检查这些项：

```yaml
codex:
  home: ".codex_home"
  model: "gpt-5.5"

feishu:
  enabled: true
  poll_interval: 5

paths:
  experiment_dir: "baseline"
  runs_dir: "runs"
  skills_dir: "skills"

mcp:
  git:
    enabled: true
    scope: "baseline_model"
    repo_path: "../ForecastModel_worktree"
    baseline_dir: "baseline"
    allowed_paths:
      - "baseline/src/**"
      - "baseline/requirements.txt"
    remote: "forecastops"
    base_branch: "ForecastModel"
    sync_on_loop_start: true
    sync_before_each_trial: false
    push_on_keep: true
    push_target_branch: "ForecastModel"
    require_human_approval_for_push: true
```

重点备注：

- `paths.experiment_dir` 或命令行 `--experiment baseline` 指向本项目的实验 baseline，用于读取 `baseline/data` 和 `baseline/outputs`。
- `mcp.git.repo_path + mcp.git.baseline_dir` 指向模型 Git worktree，默认是 `../ForecastModel_worktree/baseline`，T2 Copy 会优先从这里复制完整模型源码。
- `allowed_paths` 是 Git MCP 发布边界，KEEP 时只允许写回 `baseline/src/**` 和 `baseline/requirements.txt`。
- `push_on_keep: true` 会在 KEEP 后尝试提交/推送模型代码；生产环境建议保留 `require_human_approval_for_push: true`。

### 3. 配置 `flow_paths.yaml`

`flow_paths.yaml` 放路径契约。冷启动通常保持默认即可：

```yaml
data:
  mode: "reference_only"
  primary: "baseline/data/dish_package_feature_df.csv"
  auxiliary:
    - "baseline/data/holiday_imformation.csv"

trial:
  code_dir: "candidate/code"
  outputs_dir: "outputs/real_outputs"
  evaluation_dir: "evaluation"
  logs_dir: "logs"

global_artifacts:
  model_snapshots_dir: "runs/model_code_snapshots"
  git_action_log: "runs/git_action_log.jsonl"
  feishu_action_log: "runs/feishu_card_actions.jsonl"
```

如果本机目录和默认路径不同，创建 `flow_paths.local.yaml` 覆盖本机路径，不要把本机私有路径提交到仓库。

### 4. 配置环境变量

密钥和敏感信息通过环境变量设置，不建议写入 YAML。

Bash 当前 session 临时设置：

```bash
export OPENAI_API_KEY="sk-xxx"
export CODEX_API_KEY="xxx"
export CODEX_MODEL="gpt-5.5"
export CODEX_HOME="/srv/codex/Codex_flow/.codex_home"

export FEISHU_APP_ID="cli_xxx"
export FEISHU_APP_SECRET="xxx"
export FEISHU_CHAT_ID="oc_xxx"
export FEISHU_VERIFICATION_TOKEN="xxx"
```

服务器持久化建议写入运维侧环境注入机制，例如 shell profile、systemd `EnvironmentFile` 或 CI/CD secret。使用 `.env` 文件时，需要在启动前显式 source：

```bash
set -a
source .env
set +a
```

环境变量说明：

| 环境变量 | 是否必需 | 用途 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 视 SDK 认证方式而定 | OpenAI API Key |
| `CODEX_API_KEY` | 视 SDK 认证方式而定 | Codex API Key |
| `CODEX_MODEL` | 否 | 覆盖 `flow_config.yaml` 的 Codex 模型 |
| `CODEX_HOME` | 否 | 覆盖 Codex 登录目录，默认 `.codex_home` |
| `FEISHU_APP_ID` | 启用飞书时必需 | 飞书应用 ID |
| `FEISHU_APP_SECRET` | 启用飞书时必需 | 飞书应用密钥 |
| `FEISHU_CHAT_ID` | 启用飞书通知时必需 | 飞书目标群聊 |
| `FEISHU_VERIFICATION_TOKEN` | 启用卡片回调时必需 | 飞书卡片回调校验 token |

### 5. 准备模型 Git worktree

默认配置要求模型仓库在项目同级目录：

```text
/srv/codex/
  Codex_flow/
  ForecastModel_worktree/
    baseline/
      src/
      requirements.txt
```

如果还没有 worktree，先拉取或创建模型仓库目录，并确认远端名和分支与 `flow_config.yaml` 一致：

```bash
cd /srv/codex
git clone <ForecastModel 仓库地址> ForecastModel_worktree
cd ForecastModel_worktree
git remote -v
git checkout ForecastModel
git pull forecastops ForecastModel
```

如果在 sandbox 或服务用户下手工执行 `git -C /srv/codex/ForecastModel_worktree ...` 遇到 `dubious ownership`，只读排查可用单次参数：

```bash
git -c safe.directory=/srv/codex/ForecastModel_worktree \
  -C /srv/codex/ForecastModel_worktree \
  status --short --branch
```

Codex Flow 的 Git MCP 自身会记录同步结果到 `runs/git_action_log.jsonl`，应看到 `sync_remote_base`、`snapshot_baseline_model` 等动作。

### 6. 登录 Codex 并验证

```bash
source venv/bin/activate
python codex_login.py --logout
python codex_login.py
```

最小化验证（不依赖额外测试包）：

```bash
python -m unittest test_trial_code_layout.py
```

完整测试可使用 pytest；冷启动环境如果尚未安装 pytest，先执行 `pip install pytest`：

```bash
pip install pytest
python -m pytest -q --ignore=test_feishu_review.py
```

### 7. 启动命令

单轮实验：

```bash
source venv/bin/activate
python codex_flow.py \
  --experiment baseline \
  --ask "分析预测误差，提出特征实验并验证" \
  --output runs/trial_001
```

多轮自动优化。Git MCP 启用时会在 loop 启动时同步 `mcp.git.repo_path` 指向的模型 worktree；飞书启用时会生成审核卡片并等待人工命令或超时：

```bash
source venv/bin/activate
python loop.py \
  --experiment baseline \
  --ask "从 ForecastModel 分支同步 baseline 模型代码，基于当前 baseline 进行预测优化实验" \
  --output runs/001 \
  --max-trials 3 \
  --review-timeout 6000
```

无人值守 smoke run 可关闭人工审核：

```bash
source venv/bin/activate
python loop.py \
  --experiment baseline \
  --ask "执行一次保守预测优化 smoke test" \
  --output runs/smoke_001 \
  --max-trials 1 \
  --no-review
```

飞书卡片回调服务单独启动。需要先把公网网关或内网穿透地址映射到 `/feishu/card`：

```bash
source venv/bin/activate
python lark_card_bot.py --host 0.0.0.0 --port 8787
```

## 快速开始

```bash
# 1. 安装依赖
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

# 2. 登录 Codex，一次登录后 session 会持久化到 .codex_home
python codex_login.py --logout
python codex_login.py

# 3. 如启用 Git MCP，确认模型源码 worktree 已同步
#    默认源码位置: ../ForecastModel_worktree/baseline/src
#    loop.py 启动时会按 flow_config.yaml 自动同步远端分支。

# 4. 运行单次实验
python codex_flow.py \
  --experiment baseline \
  --ask "分析预测误差，提出特征实验并验证" \
  --output runs/trial_001

# 5. 运行多轮自动优化
python loop.py \
  --experiment baseline \
  --ask "持续优化预测模型，降低 WAPE" \
  --max-trials 10 \
  --review-timeout 1800
```

如需启用飞书通知和卡片审核，先设置环境变量：

```bash
export FEISHU_APP_ID="cli_xxx"
export FEISHU_APP_SECRET="xxx"
export FEISHU_CHAT_ID="oc_xxx"
export FEISHU_VERIFICATION_TOKEN="xxx"
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
| Copy | Code Snapshot | Python | 将允许范围内的模型代码复制到 `candidate/code/`。非链式 trial 在 Git MCP 启用时优先从 `mcp.git.repo_path` 下的 `baseline/` 复制源码；数据和历史输出只做引用。 |
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

模型源码有两个不同角色的目录：

```text
../ForecastModel_worktree/baseline/
  src/                         # Git MCP 同步的模型源码，T2 Copy 的优先来源
  requirements.txt

baseline/
  data/                        # Codex Flow 固定数据源
  outputs/                     # baseline 指标读取来源
```

运行 `codex_flow.py --experiment baseline` 或 `loop.py --experiment baseline` 时，`--experiment` 仍指向当前项目的 `baseline/`，用于数据引用和 baseline outputs。候选模型代码的初始复制来源会按配置优先选择 `mcp.git.repo_path + mcp.git.baseline_dir`，默认即 `../ForecastModel_worktree/baseline/`；只有该 worktree 不存在或未包含 `src/requirements.txt` 时，才回退到 `--experiment` 目录。

候选代码和有效模型代码严格隔离：

```text
../ForecastModel_worktree/baseline/src/   # 当前有效模型代码的 Git MCP worktree 副本
../ForecastModel_worktree/baseline/requirements.txt

runs/<run>/trial_xxx/
  candidate/code/              # 当前 trial 候选代码
  outputs/real_outputs/        # 当前 trial 输出
  evaluation/                  # 当前 trial 指标
  reports/                     # 当前 trial 报告
  logs/                        # 当前 trial 日志
```

KEEP 发布时只允许将候选代码应用回 Git MCP worktree 内的模型 baseline：

```text
baseline/src/**
baseline/requirements.txt
```

以上路径相对于 `mcp.git.repo_path`，默认是 `../ForecastModel_worktree/`。数据、日志、训练输出和报告不会进入模型发布路径。

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
    scope: "baseline_model"
    repo_path: "../ForecastModel_worktree"
    baseline_dir: "baseline"
    trial_code_subdir: "candidate/code"
    remote: "forecastops"
    base_branch: "ForecastModel"
    allowed_paths:
      - "baseline/src/**"
      - "baseline/requirements.txt"
```

`mcp.git.repo_path` 指向真正的模型 Git worktree。loop 启动同步远端后，非链式 trial 的 Copy 阶段会优先从 `{repo_path}/{baseline_dir}` 复制模型源码到 `runs/<run>/trial_xxx/candidate/code/`；`paths.experiment_dir` 或 CLI 的 `--experiment baseline` 仍用于读取本项目内的数据和 baseline outputs。

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

```bash
source venv/bin/activate
python codex_flow.py \
  --experiment baseline \
  --ask "分析预测误差并提出优化方案" \
  --output runs/trial_001
```

当 Git MCP 启用且 `../ForecastModel_worktree/baseline` 存在时，上述单次实验的 T2 Copy 阶段会从该 Git worktree 复制模型源码；`--experiment baseline` 继续用于数据源和 baseline outputs。

### 链式实验

```bash
source venv/bin/activate
python codex_flow.py \
  --experiment baseline \
  --ask "基于上一轮结果继续优化" \
  --output runs/trial_002 \
  --previous-trial runs/trial_001
```

### 多轮实验

```bash
source venv/bin/activate
python loop.py \
  --experiment baseline \
  --ask "持续优化预测模型" \
  --output runs \
  --max-trials 10 \
  --target-wape 0.55
```

多轮 loop 启动时会按 `flow_config.yaml` 的 Git MCP 配置同步 `repo_path` 指向的模型 worktree。每个非链式 trial 从该 worktree 初始化 `candidate/code/`；ROLLBACK 不发布，KEEP 才会把候选模型代码应用回同一个 Git worktree 并按配置提交/推送。

### 关闭人工审核

```bash
source venv/bin/activate
python loop.py \
  --experiment baseline \
  --ask "全自动优化预测模型" \
  --max-trials 5 \
  --no-review
```

### 启动飞书卡片回调服务

```bash
source venv/bin/activate
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

这些路径相对于 `mcp.git.repo_path`，默认 worktree 是：

```text
../ForecastModel_worktree/
```

因此模型代码链路是：

```text
远端 ForecastModel 分支
  -> Git MCP sync 到 ../ForecastModel_worktree/baseline/
  -> T2 Copy 到 runs/<run>/trial_xxx/candidate/code/
  -> T3 执行候选代码
  -> KEEP 时 apply/commit/push 回 ../ForecastModel_worktree/baseline/
```

当前项目内的 `baseline/data/` 和 `baseline/outputs/` 仍是实验数据与 baseline 指标来源，不是 Git MCP 模型源码同步目标。

非 KEEP 决策不会推送远程仓库。当前配置默认面向 `forecastops/ForecastModel` 远端分支，`create_pr_on_keep` 默认关闭，避免误用 GitHub PR 逻辑操作 GitLab 仓库。

Git 动作日志写入：

```text
runs/git_action_log.jsonl
```

## 测试

当前测试覆盖路径配置、trial 代码布局、loop dry-run、阶段恢复、Git 发布和 MCP 适配：

```bash
source venv/bin/activate
python -m unittest \
  test_path_config.py \
  test_trial_code_layout.py \
  test_agent_loop_dryrun.py \
  test_mcp_adapters.py \
  test_stage_recovery_flow.py \
  test_git_publish_flow.py
```

也可以运行单个测试文件，例如：

```bash
source venv/bin/activate
python -m unittest test_path_config.py
```

## 相关文档

- `PROJECT.md`：更完整的架构、状态机、恢复机制和发布链路说明。
- `LARK_CARD_BOT.md`：飞书卡片回调服务配置和本地测试说明。
- `REPORT.md` / `SUMMARY.md` / `TECHNICAL_PROPOSAL.md`：历史报告、总结和技术方案材料。
