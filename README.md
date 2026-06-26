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

模型训练依赖由模型仓库配置里的 `mcp.git.model.requirements_paths` 管理。该字段可以为空，也可以包含多个 requirements 文件；若要真实执行 T3 训练，需要按目标模型仓库安装对应依赖，例如：

```bash
pip install -r ../SupplyChain_worktree/src/supply_chain/forecast/package_forecast/requirements.txt
```

如果目标模型没有 requirements 文件，先确认训练入口的运行环境依赖已由镜像、虚拟环境或调度环境提供。

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
    active_repo: "supply_chain_package_forecast"
    repositories:
      supply_chain_package_forecast:
        repo:
          path: "../SupplyChain_worktree"
          url: "ssh://git@172.18.254.56:221/haidilao-algo/forecasting/supply_chain.git"
          lifecycle: "clone_if_missing"   # 路径不存在时用 repo.url 自动 clone
          remote: "origin"
          base_branch: "develop"
          sync_strategy: "ff_only"
        model:
          root: "src/supply_chain/forecast/package_forecast"
          copy_include: ["**"]
          copy_exclude: ["__pycache__/**", "*.pyc", ".git/**", "data/**", "outputs/**", "logs/**"]
          publish_paths:
            - "src/supply_chain/forecast/package_forecast/**"
          requirements_paths:
            - "requirements.txt"
          entrypoint_candidates:
            - "train.py"
            - "main.py"
            - "run.py"
            - "scripts/train.py"
          default_train_command: []
          output_contract:
            prediction_path: "{trial_id}_package_detail.csv"
            split_column: "split"
            split_filter: "test"
            actual_candidates: ["true_pos_cnt", "true_real_qty_sum", "actual", "y_true"]
            prediction_candidates: ["pred_pos_cnt", "pred_real_qty_sum", "prediction", "y_pred"]
            baseline_prediction_globs: ["*_package_detail.csv", "*.csv"]
            secondary_metric_globs: ["*_store_dish_day.csv"]
        publish:
          mode: "direct_branch"            # 或 branch_pr
          branch_prefix: "model-exp/"
          target_branch: "develop"
          push_on_keep: true
          create_pr: false
          pr_draft: true
    sync_on_loop_start: true
    sync_before_each_trial: false
    require_human_approval_for_push: true
```

重点备注：

- `paths.experiment_dir` 或命令行 `--experiment baseline` 指向本项目的实验 baseline，用于读取 `baseline/data` 和 `baseline/outputs`。
- `mcp.git.repo.path + mcp.git.model.root` 指向模型 Git worktree 中的模型根目录；T2 Copy 会从这里按 `copy_include/copy_exclude` 复制源码到 `candidate/code/`。
- `model.publish_paths` 是 Git MCP 发布边界，KEEP 时只允许把候选代码写回这些路径。
- `output_contract` 是 T3 读取预测文件、split、真实值列、预测列和 baseline 指标文件的契约；新模型接入时必须优先补齐这里。
- `repo.lifecycle` 当前示例使用 `clone_if_missing`；当 `repo.path` 不存在且提供 `repo.url` 时会尝试自动 clone。
- `push_on_keep: true` 只表示允许发布；生产环境建议保留 `require_human_approval_for_push: true`，未经过人工 KEEP 时不会执行 remote push。

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
# Optional: only needed for HTTP callback compatibility
# export FEISHU_VERIFICATION_TOKEN="xxx"
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
| `FEISHU_VERIFICATION_TOKEN` | HTTP 回调兼容入口可选 | 飞书卡片回调校验 token |

### 5. 准备模型 Git worktree

默认测试配置要求 supply_chain 模型仓库在项目同级目录，模型根目录由 `mcp.git.model.root` 指定：

```text
/srv/codex/
  Codex_flow/
  SupplyChain_worktree/
    src/supply_chain/forecast/package_forecast/
      requirements.txt  # 可选，按 requirements_paths 配置
```

如果还没有 worktree，先拉取或创建模型仓库目录，并确认远端名和分支与 `flow_config.yaml` 一致：

```bash
cd /srv/codex
git clone ssh://git@172.18.254.56:221/haidilao-algo/forecasting/supply_chain.git SupplyChain_worktree
cd SupplyChain_worktree
git remote -v
git checkout develop
git pull origin develop
```

如果在 sandbox 或服务用户下手工执行 `git -C /srv/codex/SupplyChain_worktree ...` 遇到 `dubious ownership`，只读排查可用单次参数：

```bash
git -c safe.directory=/srv/codex/SupplyChain_worktree \
  -C /srv/codex/SupplyChain_worktree \
  status --short --branch
```

Codex Flow 的 Git MCP 自身会记录同步结果到 `runs/git_action_log.jsonl`，应看到 `sync_remote_base`、`snapshot_baseline_model` 等动作。

### 6. 登录 Codex 并验证

```bash
source venv/bin/activate
python3 codex_login.py --logout
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

多轮自动优化。Git MCP 启用时会在 loop 启动时同步 `mcp.git.repo.path` 指向的模型 worktree，并只管理 `model.publish_paths`；飞书启用时会生成审核卡片并等待人工命令或超时：

```bash
source venv/bin/activate
python loop.py \
  --experiment baseline \
  --ask "从 supply_chain develop 分支同步 package_forecast 模型代码，基于当前配置进行预测优化实验" \
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

飞书消息和卡片点击推荐通过 SDK 长连接接入，不需要公网网关或内网穿透。先启动长连接进程，再运行 `loop.py`：

```bash
source venv/bin/activate
python lark_channel_bot.py
```

也可以单独发送一张测试审核卡片：

```bash
source venv/bin/activate
python lark_channel_bot.py --send-test-card
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
#    默认源码位置: ../SupplyChain_worktree/src/supply_chain/forecast/package_forecast
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
# Optional: only needed for HTTP callback compatibility
# export FEISHU_VERIFICATION_TOKEN="xxx"
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
  lark_channel_bot.py          # 飞书 SDK 长连接消息/卡片接收服务
  lark_card_bot.py             # 飞书交互卡片 HTTP 回调兼容服务
  card_server.py               # HTTP 卡片服务兼容入口
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
| Copy | Code Snapshot | Python | 从 `mcp.git.repo.path + model.root` 按 `copy_include/copy_exclude` 复制模型代码到 `candidate/code/`；数据和历史输出只做引用。 |
| T2b | Codegen | Codex | 基于注入的 `model_contract` 生成或改造候选训练代码，默认写入 `runs/<run>/trial_xxx/candidate/code/`，并产出 `agent2_execution_plan.yaml`。 |
| T3 | Execute | Python | 执行 `agent2_execution_plan.yaml` 中的 `train_command`，按 `output_contract` 读取预测文件、split、actual/prediction 列并计算指标。 |
| T4 | Report | Codex | 生成中文实验报告。该阶段可降级，失败不会直接毁掉整轮实验。 |
| T5 | Feishu Card | Codex/Python | 生成并发送飞书审核卡片。该阶段可降级，支持后续人工恢复。 |

T3 是唯一真正执行训练和评估的阶段。日志中会明确出现 `[T3] 执行训练`，用于避免 T1/T2 阶段误跑长任务。T1/T2 不应依赖固定模型入口；所有入口候选、训练命令和输出契约都来自 `model_contract`。

最新执行链路：

```text
loop 启动
  -> Git MCP 校验/同步 repo.path 的 worktree（当前 clone_if_missing + ff_only）
  -> 每轮创建 trial 分支和模型快照
  -> Copy: model.root 按 copy_include/copy_exclude 复制到 candidate/code/
  -> T2b: Codex 基于 model_contract 生成 train.py + agent2_execution_plan.yaml
  -> T3: Python 执行 train_command，并按 output_contract 评估 prediction/baseline
  -> T4/T5: 生成报告和飞书审核卡片
  -> KEEP: apply candidate/code 回 publish_paths，commit；人工 KEEP 通过后才 remote push/PR
  -> ROLLBACK/REVERSE: 丢弃未接受改动或恢复模型快照
```

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
../SupplyChain_worktree/
  src/supply_chain/forecast/package_forecast/   # Git MCP model.root，T2 Copy 的优先来源
    requirements.txt                            # 可选；也可由 requirements_paths 配置多个文件

baseline/
  data/                                        # Codex Flow 固定数据源
  outputs/                                     # baseline 指标读取来源
```

运行 `codex_flow.py --experiment baseline` 或 `loop.py --experiment baseline` 时，`--experiment` 仍指向当前项目的 `baseline/`，用于数据引用和 baseline outputs。候选模型代码的初始复制来源会按配置优先选择 `mcp.git.repo.path + mcp.git.model.root`；如果 Git MCP 未启用或该模型根目录不可用，才回退到 `--experiment` 目录。

候选代码和有效模型代码严格隔离：

```text
../SupplyChain_worktree/src/supply_chain/forecast/package_forecast/  # 当前有效模型代码

runs/<run>/trial_xxx/
  candidate/code/              # 当前 trial 候选代码，结构与 model.root 同构
  outputs/real_outputs/        # 当前 trial 输出
  evaluation/                  # 当前 trial 指标
  reports/                     # 当前 trial 报告
  logs/                        # 当前 trial 日志
```

KEEP 发布时只允许将候选代码应用回 Git MCP worktree 内的 `model.publish_paths`：

```text
src/supply_chain/forecast/package_forecast/**
```

以上路径相对于 `mcp.git.repo.path`。数据、日志、训练输出和报告不会进入模型发布路径。

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
    active_repo: "supply_chain_package_forecast"
    repositories:
      supply_chain_package_forecast:
        repo:
          path: "../SupplyChain_worktree"
          url: "ssh://git@172.18.254.56:221/haidilao-algo/forecasting/supply_chain.git"
          lifecycle: "clone_if_missing"
          remote: "origin"
          base_branch: "develop"
          sync_strategy: "ff_only"
        model:
          root: "src/supply_chain/forecast/package_forecast"
          copy_include: ["**"]
          copy_exclude: ["__pycache__/**", "*.pyc", ".git/**", "data/**", "outputs/**", "logs/**"]
          publish_paths:
            - "src/supply_chain/forecast/package_forecast/**"
          requirements_paths:
            - "requirements.txt"
          entrypoint_candidates: ["train.py", "main.py", "run.py", "scripts/train.py"]
          default_train_command: []
          output_contract:
            prediction_path: "{trial_id}_package_detail.csv"
            split_column: "split"
            split_filter: "test"
            actual_candidates: ["true_pos_cnt", "true_real_qty_sum", "actual", "y_true"]
            prediction_candidates: ["pred_pos_cnt", "pred_real_qty_sum", "prediction", "y_pred"]
            baseline_prediction_globs: ["*_package_detail.csv", "*.csv"]
            secondary_metric_globs: ["*_store_dish_day.csv"]
        publish:
          mode: "direct_branch"
          branch_prefix: "model-exp/"
          target_branch: "develop"
          push_on_keep: true
          create_pr: false
          pr_draft: true
```

`mcp.git.repo.path` 指向真正的模型 Git worktree。loop 启动同步远端后，非链式 trial 的 Copy 阶段会优先从 `{repo.path}/{model.root}` 按 `copy_include/copy_exclude` 复制模型源码到 `runs/<run>/trial_xxx/candidate/code/`；`paths.experiment_dir` 或 CLI 的 `--experiment baseline` 仍用于读取本项目内的数据和 baseline outputs。

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
| `FEISHU_VERIFICATION_TOKEN` | 飞书 HTTP 回调校验 token，长连接主路径可不设 |

## 常用命令

### 单次实验

```bash
source venv/bin/activate
python codex_flow.py \
  --experiment baseline \
  --ask "分析预测误差并提出优化方案" \
  --output runs/trial_001
```

当 Git MCP 启用且 `repo.path + model.root` 存在时，上述单次实验的 Copy 阶段会从该 Git worktree 复制模型源码；`--experiment baseline` 继续用于数据源和 baseline outputs。

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

多轮 loop 启动时会按 `flow_config.yaml` 的 Git MCP 配置同步 `repo.path` 指向的模型 worktree。每个非链式 trial 从 `{repo.path}/{model.root}` 初始化 `candidate/code/`；ROLLBACK 不发布，KEEP 才会把候选模型代码应用回同一个 Git worktree 的 `publish_paths` 并按配置提交。若开启 `require_human_approval_for_push`，只有人工 KEEP 才允许 remote push/PR。

### 关闭人工审核

```bash
source venv/bin/activate
python loop.py \
  --experiment baseline \
  --ask "全自动优化预测模型" \
  --max-trials 5 \
  --no-review
```

### 启动飞书 SDK 长连接服务

```bash
source venv/bin/activate
python lark_channel_bot.py
```

该进程通过 `lark-oapi` 的 WebSocket 长连接接收文本消息和卡片按钮事件，并写入本地动作日志；不需要配置公网回调地址或内网穿透。

发送测试卡片：

```bash
source venv/bin/activate
python lark_channel_bot.py --send-test-card
```

HTTP 回调服务仍保留为兼容入口。只有使用该入口时，才需要在飞书开发者后台配置卡片回调地址：

```text
https://<public-host>/feishu/card
```

```bash
source venv/bin/activate
python lark_card_bot.py --host 0.0.0.0 --port 8787
```

更多细节见 `LARK_CARD_BOT.md`。

## 人工审核与恢复命令

飞书审核支持文本命令和交互卡片按钮。长连接服务和 HTTP 兼容回调都会把动作写入：

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

KEEP 后可通过 `git_subagent.py` 和 `mcp_servers/git_research_server/` 发布模型代码。Git 工具被限制在 `model.publish_paths` 内，当前 supply_chain/package_forecast 示例为：

```text
src/supply_chain/forecast/package_forecast/**
```

这些路径相对于 `mcp.git.repo.path`，当前示例 worktree 是：

```text
../SupplyChain_worktree/
```

因此模型代码链路是：

```text
远端 origin/develop
  -> Git MCP sync 到 ../SupplyChain_worktree/（ff-only，不 reset，不 force push）
  -> Copy: src/supply_chain/forecast/package_forecast 按 copy_include/copy_exclude 复制到 candidate/code/
  -> T2b: Codex 基于 model_contract 生成 train.py 和 agent2_execution_plan.yaml
  -> T3: 执行 train_command，并按 output_contract 计算新旧指标
  -> KEEP: apply candidate/code 回 publish_paths，commit 到当前 trial 分支
  -> 人工 KEEP: 才允许 push direct_branch 或 branch_pr；未人工 KEEP 时只保留本地 commit/草稿
```

当前项目内的 `baseline/data/` 和 `baseline/outputs/` 仍是实验数据与 baseline 指标来源，不是 Git MCP 模型源码同步目标。

非 KEEP 决策不会推送远程仓库。`publish.mode=direct_branch` 会保持当前直接推目标分支能力；`publish.mode=branch_pr` 会推实验分支并生成 PR/MR 草稿路径。GitHub PR 由 `gh pr create` 处理；GitLab 仓库建议先使用草稿或后续接入 GitLab MR API。

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
- `LARK_CARD_BOT.md`：飞书 HTTP 卡片回调兼容入口配置和本地测试说明；主路径优先使用 `lark_channel_bot.py` 长连接。
- `REPORT.md` / `SUMMARY.md` / `TECHNICAL_PROPOSAL.md`：历史报告、总结和技术方案材料。
