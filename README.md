# ForeCast by Codex Flow

Codex Flow 是一个面向预测模型迭代的自动化实验系统。它把 Codex 的诊断、规划、代码生成和报告能力，与本地 Python 训练评估、飞书人工审核、阶段恢复、Git MCP 发布链路组合成一条可追踪、可恢复、可审计的多轮优化流程。

系统的核心边界很清楚：

- Codex 负责分析问题、设计实验、生成候选代码和撰写报告。
- 本地编排层负责训练、评估、KEEP/ROLLBACK/REVERSE 决策、checkpoint、路径隔离和 Git 发布。
- 飞书链路负责把关键节点交给人审核，保留人工确认入口。

## 能力概览

| 模块 | 作用 |
| --- | --- |
| `codex_flow.py` | 主工作流编排入口，串联评估、规划、代码生成、训练、报告和审核。 |
| `config.py` | 统一配置加载，支持 YAML 配置和环境变量覆盖。 |
| `codex_login.py` | Codex 设备码登录与会话持久化。 |
| `codex_gateway.py` | Codex 网络代理/Cloudflare Access 网关辅助工具。 |
| `mcp_servers/git_research_server/` | Git MCP 服务，用于模型代码 diff、应用、发布和回滚。 |
| `lark_card_bot.py` / `lark_channel_bot.py` | 飞书卡片回调与频道交互入口。 |
| `skills/` | 面向预测实验的可复用 Codex Skills。 |
| `runs/` | 每轮实验输出、日志、报告和审计文件。 |

## 快速开始

### 1. 创建运行环境

```bash
cd /path/to/Codex_flow
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

模型训练依赖由目标模型仓库里的 `requirements.txt` 管理。当前默认配置指向 SupplyChain worktree：

```bash
pip install -r ../SupplyChain_worktree/src/supply_chain/forecast/package_forecast/requirements.txt
```

### 2. 配置 Codex 会话目录

默认会话目录在项目内：

```bash
export CODEX_HOME=/data/xujuanyi/CodexFlow/forecastops-agent/.codex_home
```

目录中会保存 Codex 登录态和本地运行状态，请不要提交 `.codex_home/`。

### 3. 配置网络代理

如果服务器不能直连 Codex，需要先确认代理端口从服务器视角可用。

复用服务器已有代理：

```bash
export HTTPS_PROXY="http://127.0.0.1:7890"
export HTTP_PROXY="$HTTPS_PROXY"
export ALL_PROXY="$HTTPS_PROXY"
```

通过 SSH 把本机代理转发到服务器：

```bash
ssh -N \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -R 127.0.0.1:8787:127.0.0.1:7890 \
  xujuanyi@服务器地址
```

然后在服务器上使用：

```bash
export HTTPS_PROXY="http://127.0.0.1:8787"
export HTTP_PROXY="$HTTPS_PROXY"
export ALL_PROXY="$HTTPS_PROXY"
```

验证代理：

```bash
curl -v -x "$HTTPS_PROXY" https://auth.openai.com/codex/device
curl -i --http1.1 -x "$HTTPS_PROXY" https://chatgpt.com/backend-api/codex/responses
```

### 4. 登录 Codex

```bash
python3 codex_login.py --logout
python3 codex_login.py
```

也可以直接测试一个最小 Codex 线程：

```bash
python3 start_codex_thread.py "回复 OK"
```

## 关键配置

主配置文件是 `flow_config.yaml`，敏感信息通过环境变量注入。

```yaml
codex:
  home: ".codex_home"
  model: "gpt-5.5"

codex_gateway:
  enabled: true
  mode: "proxy_env_only"
  proxy_url: ""

mcp:
  git:
    enabled: true
    scope: "baseline_model"
    active_repo: "supply_chain_package_forecast"
    repo_path: "../SupplyChain_worktree"
    baseline_dir: "src/supply_chain/forecast/package_forecast"
    allowed_paths:
      - "src/supply_chain/forecast/package_forecast/**"
```

常用环境变量：

| 变量 | 说明 |
| --- | --- |
| `CODEX_HOME` | Codex 状态目录，默认 `.codex_home`。 |
| `CODEX_MODEL` | 覆盖 `flow_config.yaml` 中的模型名。 |
| `OPENAI_API_KEY` / `CODEX_API_KEY` | API key 登录方式需要时使用。 |
| `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` | 服务器访问 Codex 的代理配置。 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_CHAT_ID` | 飞书通知与审核卡片配置。 |
| `FEISHU_VERIFICATION_TOKEN` / `FEISHU_ENCRYPT_KEY` | 飞书回调验签与加密消息配置。 |

## 运行工作流

最小运行：

```bash
python3 codex_flow.py \
  --experiment baseline \
  --objective "优化 package forecast 的 WAPE 和 Bias" \
  --output-dir runs/trial_001
```

常见产物：

| 路径 | 内容 |
| --- | --- |
| `runs/<trial>/agent1/` | 诊断、实验假设、计划和上下文。 |
| `runs/<trial>/candidate/code/` | Codex 生成的候选模型代码。 |
| `runs/<trial>/evaluation/` | 指标对比、KEEP/ROLLBACK/REVERSE 决策依据。 |
| `runs/<trial>/reports/` | 面向人工审核的实验报告。 |
| `runs/git_action_log.jsonl` | Git MCP 发布和回滚审计日志。 |

## 飞书审核入口

启动卡片回调服务：

```bash
python3 lark_card_bot.py --host 0.0.0.0 --port 8787
```

健康检查：

```bash
curl http://127.0.0.1:8787/health
```

飞书开发者后台的回调地址配置为：

```text
https://<public-host>/feishu/card
```

## 测试

运行单元测试：

```bash
python -m unittest discover -v
```

快速语法检查：

```bash
python -m py_compile \
  config.py \
  codex_flow.py \
  codex_login.py \
  codex_gateway.py \
  lark_card_bot.py \
  start_codex_thread.py
```

## 提交前检查

建议每次提交前确认：

```bash
git status --short
git diff --check
python -m unittest discover -v
```

不要提交以下内容：

- `.codex_home/`
- `.env`
- API key、飞书 app secret、Codex access token
- 大型运行产物或临时缓存

## 典型目录结构

```text
Codex_flow/
  baseline/                  # 基线数据、输出和历史实验入口
  mcp_servers/               # Git MCP 服务
  runs/                      # 实验运行产物
  skills/                    # 预测实验 Skills
  codex_flow.py              # 主编排入口
  codex_login.py             # Codex 登录入口
  flow_config.yaml           # 主配置
  flow_paths.yaml            # 路径契约
```

## 发布策略

`KEEP` 只是允许发布的业务决策，不等于无条件推送。生产环境建议保持：

```yaml
require_human_approval_for_push: true
allow_force_push: false
allow_reset_hard: false
```

这样可以让 Codex 负责探索，让本地编排层和人工审核共同守住发布边界。
