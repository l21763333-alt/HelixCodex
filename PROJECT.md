# Codex Flow 项目介绍

Codex Flow 是一个面向预测模型迭代的自动化实验系统。它把 Codex SDK 的分析和代码生成能力、Python 的确定性训练执行、飞书人工审核、Git MCP 发布链路组合成一条可追踪、可恢复、可审计的多轮优化流程。

系统的目标不是让大模型直接接管训练，而是让大模型负责诊断、设计实验、生成候选代码和撰写报告；训练、评测、KEEP/ROLLBACK 判断、checkpoint、Git 发布等关键动作由本地 Python 编排层明确执行。

## 核心架构

主要模块如下：

| 模块 | 作用 |
| --- | --- |
| `codex_flow.py` | 单轮 trial 工作流，负责 T1-T5 阶段执行、manifest、恢复和评测产物管理 |
| `loop.py` | 多轮实验编排器，负责 round 循环、KEEP/ROLLBACK/REVERSE、checkpoint、飞书审核和 Git 发布 |
| `config.py` | 行为配置和路径配置加载，提供 `FlowPathsConfig` / `PathRegistry` |
| `flow_config.yaml` | 行为配置，例如 Codex、恢复策略、飞书、Git MCP、loop 策略 |
| `flow_paths.yaml` | 路径配置，例如 baseline、固定数据源、trial 目录、全局产物目录 |
| `baseline/` | 当前有效模型代码和固定训练数据源 |
| `runs/` | 每次运行和每轮 trial 的实验产物、日志、报告、checkpoint |
| `mcp_servers/git_research_server/` | 真 MCP Git server，封装受限 Git 操作工具 |
| `git_subagent.py` | Git subagent 入口，通过 Codex thread + Git MCP 执行同步、提交、推送 |
| `lark_notify.py` / `lark_card_bot.py` | 飞书通知、审核卡片、恢复卡片和按钮/命令回调 |

## 阶段流程

单轮实验由 `codex_flow.py` 拆成多个阶段：

| 阶段 | 名称 | 说明 |
| --- | --- | --- |
| T1 | Evaluate | 只做诊断和标准化产物生成。禁止训练、禁止生成 candidate、禁止修改模型代码、禁止长任务 |
| T2a | Plan | 根据 T1 产物制定实验计划、特征假设和候选方向 |
| Source Copy | Code Snapshot | 只复制模型代码白名单到 trial 候选代码目录，不复制数据和训练输出 |
| T2b | Codegen | 生成候选训练代码，默认写入 `candidate/code/` |
| T3 | Execute | 由 Python 明确打印并执行训练命令，读取固定数据源，输出到 trial 独立目录 |
| T4 | Report | 生成实验报告。该阶段可降级，失败不会直接毁掉整轮实验 |
| T5 | Feishu Card | 生成/发送飞书审核卡片。该阶段可降级，支持后续人工恢复 |

T3 是唯一真正执行训练评测的阶段。日志中会明确出现 `[T3] 执行训练`，避免 T1 阶段误跑训练或长任务。

## 数据与代码隔离

当前项目采用“固定数据源 + trial 引用”的结构。

训练评测数据不再复制到每个 trial。`baseline/data/` 被视为当前机器上的固定只读数据源，trial 只记录数据引用和校验信息：

```text
runs/<run>/trial_xxx/
  inputs/data_refs.json
```

`data_refs.json` 记录数据路径、大小、mtime 和轻量 hash/schema 信息，便于追溯实验使用的数据版本，同时避免每轮复制 4GB+ CSV。

候选模型代码与有效模型代码严格隔离：

```text
baseline/
  src/                  # 当前有效模型代码
  requirements.txt
  data/                 # 固定只读数据源

runs/<run>/trial_xxx/
  candidate/code/       # 当前 trial 候选代码
  outputs/real_outputs/ # 当前 trial 训练输出
  evaluation/           # 当前 trial 评测结果
  reports/              # 当前 trial 报告
  logs/                 # 当前 trial 日志
```

候选代码目录禁止包含：

```text
data/
outputs/
logs/
__pycache__/
```

KEEP 发布时只允许把 `candidate/code/src/**` 和 `candidate/code/requirements.txt` 应用回 `baseline/`。数据、日志、训练输出、报告不会进入模型发布路径。

## Manifest 与 Checkpoint

每个 trial 都会生成 `workflow_manifest.json`，记录阶段状态、路径摘要、输入引用、关键产物和降级/中断信息。

阶段状态包括：

| 状态 | 含义 |
| --- | --- |
| `completed` | 阶段完成 |
| `interrupted` | Codex SDK 或网络等问题导致阶段中断，可恢复 |
| `failed` | 阶段失败 |
| `degraded` | 阶段使用 fallback 产物继续 |

每轮完成后，`loop.py` 会保存 checkpoint：

```text
runs/<run>/trial_xxx/.checkpoint/
```

checkpoint 用于记录当前 round 决策、指标、模型快照和追溯链，支持后续继续运行和回滚。

## 阶段恢复机制

Codex SDK 调用可能因为网络、沙箱或服务侧超时中断。项目现在把这类问题从“整个 loop 失败”改为“阶段级可恢复”。

恢复机制包括：

| 能力 | 说明 |
| --- | --- |
| 自动重试 | recoverable 阶段先按配置重试 |
| 阶段中断记录 | 重试耗尽后写入 manifest 的 `interrupted` 状态 |
| 飞书恢复卡片 | 发送恢复、重跑、跳过、停止按钮 |
| 文本命令恢复 | 支持 `/resume`、`/retry-stage`、`/skip-stage`、`/stop` |
| 可降级阶段 | `report`、`feishu_card` 可跳过或 fallback 后继续 |

恢复时，`run_workflow(..., resume=True, resume_from_phase=<phase>)` 会加载已有 manifest，跳过已完成阶段，从指定阶段继续。

## KEEP / ROLLBACK / REVERSE

每轮训练完成后，T3 读取当前 trial 指标和上一轮 baseline 指标，生成自动决策。飞书人工审核可以覆盖自动决策。

| 决策 | 行为 |
| --- | --- |
| KEEP | 接受当前候选模型，保存 checkpoint，发布模型代码，作为下一轮起始模型 |
| ROLLBACK | 拒绝当前候选模型，恢复到上一轮有效模型，不推送 |
| REVERSE | 反向实验或人工指定回退逻辑，不推送 |
| STOP | 停止 loop |

非 KEEP 不会推送远程仓库。

## Git MCP 发布闭环

项目引入了真正的 Git MCP server，Git subagent 只能通过 MCP tools 操作模型代码，不允许直接 shell 执行任意 Git 命令。

当前 Git 发布边界：

```text
baseline/src/**
baseline/requirements.txt
```

loop 启动时可以同步远端 `forecastops/ForecastModel` 作为当日初始 baseline。每轮 KEEP 后，系统会：

1. 将候选代码应用到 `baseline/`。
2. 提交本地模型分支。
3. 推送到配置的远端目标分支。
4. 写入 `git_publish_result.json`。
5. 通过飞书通知 commit、branch、push 结果和下一轮起始模型。

当前配置面向 GitLab 仓库的 `ForecastModel` 分支。PR/MR 自动创建默认关闭，避免用 GitHub PR 逻辑误操作 GitLab 仓库。

## 飞书控制面

飞书用于两类人工控制：

1. 实验审核：KEEP、ROLLBACK、REVERSE、STOP。
2. 阶段恢复：RESUME、RETRY-STAGE、SKIP-STAGE、STOP。

按钮回调和文本命令都会记录到：

```text
runs/feishu_card_actions.jsonl
```

Git 操作日志记录到：

```text
runs/git_action_log.jsonl
```

## 推荐目录结构

```text
Codex_flow/
  baseline/
    src/
    requirements.txt
    data/
      dish_package_feature_df.csv
      holiday_imformation.csv

  runs/
    020/
      .loop_state.json
      trial_001/
        workflow_manifest.json
        inputs/data_refs.json
        candidate/code/
        standardized/
        outputs/real_outputs/
        evaluation/metric_comparison.json
        reports/
        logs/
        .checkpoint/
      trial_002/
        ...
    model_code_snapshots/
    feishu_card_actions.jsonl
    git_action_log.jsonl
    pr_drafts/

  mcp_servers/
    git_research_server/

  flow_config.yaml
  flow_paths.yaml
  flow_paths.local.yaml
```

`flow_paths.local.yaml` 用于本机覆盖路径，已加入 `.gitignore`，不应提交。

## 测试覆盖

当前测试重点覆盖：

| 测试文件 | 覆盖点 |
| --- | --- |
| `test_path_config.py` | 路径配置加载、解析、本地覆盖 |
| `test_trial_code_layout.py` | trial 候选代码布局、数据不复制、固定数据源 |
| `test_agent_loop_dryrun.py` | loop dry-run 和基础编排 |
| `test_stage_recovery_flow.py` | 阶段恢复、飞书恢复命令、跳过/停止 |
| `test_git_publish_flow.py` | KEEP 发布、非 KEEP 不推送、Git subagent 调用 |
| `test_mcp_adapters.py` | MCP server 工具适配 |

推荐执行：

```bash
python -m unittest test_path_config.py test_trial_code_layout.py test_agent_loop_dryrun.py test_mcp_adapters.py test_stage_recovery_flow.py test_git_publish_flow.py
```
