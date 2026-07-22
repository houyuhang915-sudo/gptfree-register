# Gptfree协议注册工具

`gptfree-register` 是一个面向 ChatGPT 协议注册任务的独立控制台。它将账号导入、批次注册、结果归档、Agent Identity、Sub2API 交付和账号状态轮询整合在一个可部署服务中，适合将注册任务、备用账号池和后续状态管理集中运行。

## 项目介绍

项目使用 `Mail Auth` 作为唯一协议内核，支持 Outlook 与 iCloud/Relay 邮箱输入；浏览器模式作为可选执行方式保留。服务独立保存配置、账号状态、任务日志和导出结果，不依赖原工作台的数据目录或运行进程。

控制台将每个账号的流程拆成“备用池、注册批次、注册后凭据、状态追踪”四个可查看的阶段。任务从账号池领取指定数量的账号，服务端持续执行并记录结果；注册后可生成 Agent Identity、导出或自动导入 Sub2API；已注册账号则由后台轮询持续更新状态和确认存活时长。

## 核心能力

- **导入账号池**：一次导入 Outlook 或 iCloud/Relay 邮箱凭据，凭据写入加密 Vault，页面只展示邮箱和状态。
- **分批注册**：从备用池按数量领取账号，服务原子预占，任务结束后自动写回已注册、失败或待重试状态。
- **Mail Auth 协议注册**：唯一内置的协议实现，支持 Outlook 和 iCloud/Relay OTP 路径。
- **浏览器注册**：可选 BitBrowser、RoxyBrowser 或本地 Chromium 执行方式。
- **Agent Identity**：注册完成后可生成 Ed25519 Agent Identity，并保存为可导出的 `auth.json` 数据。
- **Sub2API 自动导入**：任务可生成 Sub2API 导入文件，也可在填写 Sub2API 地址、API Key、接口路径和分组后，自动推送本批 Agent Identity。
- **自动状态轮询**：后台可按间隔刷新 Codex RT 并探测现有 Access Token，记录最近探测、最后确认状态与确认存活时长。
- **长期账号管理**：账号状态、轮询记录、任务日志和导出结果都持久化到挂载目录，服务重启后继续保留。

账号可用性由上游服务状态、邮箱凭据和令牌状态共同决定。控制台通过持续轮询避免把临时网络或令牌问题直接标记为账号停用，并保留已确认状态用于长期追踪；它不会承诺账号永久可用。

## 运行流程

```text
Outlook / iCloud 凭据
        |
        v
账号池导入 -> 分批预占 -> Mail Auth 或 Browser 注册
        |                                  |
        |                                  v
        |                        Agent Identity / 手机绑定
        |                                  |
        v                                  v
账号状态库 <- 自动轮询 <- Sub2API 导出或自动导入
```

## 使用教程

### 快速开始

要求：Python 3.11+、Node.js（Docker 镜像会自动安装）和可选的 Docker Compose。

```bash
git clone https://github.com/houyuhang915-sudo/gptfree-register.git
cd gptfree-register
cp .env.example .env
chmod +x start.sh
./start.sh
```

打开 <http://127.0.0.1:8866>。设置 `FREE_CONSOLE_PASSWORD` 后，控制台启用 HTTP Basic Authentication，用户名可任意填写。

本地仅检查 UI 和任务流程时，使用 Dry Run：

```bash
FREE_CONSOLE_DRY_RUN=1 python3 app.py
```

Dry Run 使用本地模拟执行器，不会发起外部注册请求。

### 控制台使用

#### 1. 配置运行环境

进入“运行配置”按需填写：

- 接码平台参数；
- 托管代理的地址、端口、用户名和密码；
- Sub2API 的 Base URL、API Key、Agent 导入接口与 Group IDs；
- 浏览器宿主地址；
- 显示时区，默认 `Asia/Shanghai`。

敏感配置保存在 `data/settings.json`，接口读取时会隐藏密钥字段。

#### 2. 导入账号池

在“账号池”中选择导入，支持以下格式：

```text
# Outlook
email@example.com----mail-password----microsoft-client-id----microsoft-refresh-token

# iCloud / Relay
relay@example.com----https://relay.example.com/otp-endpoint
```

导入后账号状态为“待注册”。新建任务时选择“账号池”，填写本批数量；服务会在启动前预占本批账号，避免并行任务重复领取。

#### 3. 创建注册任务

在“新建任务”中选择协议方式时，`Mail Auth` 是唯一选项，表示该协议同时覆盖 Outlook 和 iCloud/Relay 邮箱。设置并发、代理和注册后动作后提交任务。任务在服务端执行，关闭浏览器不影响任务继续运行。

#### 4. 生成并导入 Sub2API

选择“Agent Identity”作为注册后动作后，可启用：

1. **生成 Sub2API 导入文件**：把本批成功账号导出为可导入的 JSON。
2. **自动导入 Sub2API**：任务结束后调用运行配置中的 Codex Session 导入接口，并将结果写入任务日志。

自动导入依赖以下配置：

```dotenv
GATEWAY_SUB2API_URL=https://sub2api.example.com
GATEWAY_SUB2API_TOKEN=your-api-key
GATEWAY_SUB2API_AGENT_PATH=/api/v1/admin/accounts/import/codex-session
GATEWAY_SUB2API_GROUP_IDS=2
```

#### 5. 启用自动轮询

账号池页面的“自动轮询”支持：

- 开启或暂停后台轮询；
- 设置 15 到 1440 分钟的间隔；
- 设置 1 到 8 的探测并发；
- 选择是否先刷新 Codex RT；
- 使用“立即轮询”执行一次后台任务。

轮询仅使用已保存的 Codex RT 或 Access Token，不会发起邮箱协议登录。`401/403` 等令牌异常会记录为待复核，避免错误覆盖已确认的账号状态。确认存活时长从最后一次成功确认开始累计，并在账号池列表中展示。

### Docker 部署

```bash
cp .env.example .env
mkdir -p deploy-data/data deploy-data/output
sudo chown -R 10001:10001 deploy-data
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8866/api/health
```

默认仅绑定本机回环地址：

```dotenv
FREE_CONSOLE_PORT=8866
FREE_CONSOLE_PUBLISH_PORT=18866
FREE_CONSOLE_BIND_ADDRESS=127.0.0.1
```

如需公网访问，建议通过 `deploy/nginx.conf.example` 配置 HTTPS 反向代理，而不是直接暴露服务端口。

### systemd 部署

```bash
sudo useradd --system --home /opt/gptfree-register --shell /usr/sbin/nologin freeops
sudo install -d -o freeops -g freeops /opt/gptfree-register
sudo rsync -a --chown=freeops:freeops ./ /opt/gptfree-register/
cd /opt/gptfree-register
sudo -u freeops -H python3 -m venv .venv
sudo -u freeops -H .venv/bin/pip install -r requirements.txt
sudo -u freeops -H cp .env.example .env
sudo cp deploy/gptfree-register.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gptfree-register
```

## 数据、备份与更新

以下目录包含运行状态，应作为部署备份的一部分：

```text
data/settings.json          # 配置和密钥，权限 0600
data/status_poll.json       # 自动轮询配置
data/accounts/              # 邮箱账号池
data/account_vault/         # 加密凭据 Vault
data/pool_state.db          # 账号池状态与存活记录
data/jobs/                  # 任务元数据
output/jobs/                # 任务日志
output/results/             # 每批 JSONL 结果
output/core/                # Agent Identity、Sub2API 导出等产物
```

可生成不带运行数据和密钥的代码发布包：

```bash
./scripts/package_release.sh
```

在从 GitHub 克隆的 `gptfree-register` 目录中执行时，脚本会生成 `gptfree-register-0.1.0.tar.gz`。

## 开发检查

```bash
PYTHONPATH=. pytest -q tests
node --check static/app.js
python3 -m py_compile app.py account_registry.py core/scripts/run_email_proto_register.py
```

GitHub Actions 会对每次 push 和 pull request 运行相同的测试。

## License

[MIT](LICENSE)
