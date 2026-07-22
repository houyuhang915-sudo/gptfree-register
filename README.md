# Free Register Console

Free Register Console 是一个可独立部署的注册任务与账号状态控制台，包含响应式 Web UI、任务队列、实时日志、结果聚合、账号池和自动状态轮询。

## 项目边界

- `app.py`：独立 Flask API、任务进程、持久化状态、代理/SMS 检查。
- `templates/` + `static/`：独立运营控制台 UI。
- `core/`：Free 注册所需运行时副本。
  - protocol：Mail Auth 兼容内核，同时支持 Outlook 与 iCloud 邮箱；
  - browser：BitBrowser / RoxyBrowser / Chromium；
  - Agent Identity；
  - platform/manual 手机绑定与 Codex RT；
  - Free trial 检查、账号落库和 Sub2API Agent 导入。
- `data/`：运行配置、账号池、任务元数据和临时输入。
- `output/`：任务日志、独立 JSONL 结果和核心账号产物。

## 服务端职责

服务端提供独立运行时和持久化层：

- 加密保存备用账号凭据，并将库存、批次预占、注册结果和健康状态写入独立数据目录；
- 后台执行注册与存活检测任务，浏览器关闭或断开后任务继续运行；
- 对每批账号原子预占，避免两个任务领取同一账号；
- 聚合任务结果、日志和存活时长，页面只读取非敏感状态；
- 每个部署使用自己的数据目录、配置和运行记录。

## 账号池与存活检测

- “账号池”保存备用 Outlook / iCloud 凭据，可一次导入最多 1000 条；凭据由独立加密 Vault 管理，页面和任务列表只显示邮箱与状态。
- 从账号池创建任务时设置“本批数量”，服务会原子预占该批账号；任务结束后自动写回为“已注册”或“注册失败”，未领取账号继续留在备用池。
- 已注册账号的“确认存活时长”只累计到最后一次成功确认，不会把账号年龄当成测活结果；页面同时展示最近探测与最后确认时间。
- 检测结果会保留已确认套餐，暂时网络或令牌错误会标记为待复核，不会直接覆盖上一次确认状态；订阅过期仍显示为账号可用。
- 自动轮询默认启用，每 60 分钟、并发 4。它只检查已注册且已有 Codex RT 或现有 Access Token 的账号：可选刷新 Codex RT，随后使用 `/backend-api/me` 进行实时探测；不会触发 Outlook/Relay 协议登录。已配置托管代理时，RT 刷新与 AT 探测经该代理执行；没有 RT/AT 的已注册账号会跳过并显示在轮询摘要中。
- 所有记录以 UTC ISO 时间保存；页面默认按 `Asia/Shanghai` 显示。可在“运行配置 → 运行时”调整显示时区。

## 本地启动

```bash
cd free-register-console
cp .env.example .env
# 编辑 .env，至少设置 FREE_CONSOLE_PASSWORD 和代理配置
chmod +x start.sh
./start.sh
```

打开 `http://127.0.0.1:8866`。如果设置了 `FREE_CONSOLE_PASSWORD`，浏览器会使用 HTTP Basic Authentication；用户名可填写任意值。

开发检查可直接运行：

```bash
FREE_CONSOLE_DRY_RUN=1 python3 app.py
```

Dry-run 会使用 `scripts/fake_runner.py` 生成本地演示结果，不发起外部注册请求。

## 发布包

```bash
./scripts/package_release.sh
```

脚本会生成父目录中的 `free-register-console-0.1.0.tar.gz`，并校验归档不包含 `.env`、账号凭据、任务输入、运行日志、缓存或本机软链接。

## Docker 部署

```bash
cp .env.example .env
# 必须设置 FREE_CONSOLE_PASSWORD，并按需填写代理、SMS、Sub2API 配置
mkdir -p deploy-data/data deploy-data/output
sudo chown -R 10001:10001 deploy-data
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8866/api/health
```

Compose 默认仅绑定到 `127.0.0.1`，由 Nginx 作为公网入口。容器内端口、宿主端口和绑定地址都可以在 `.env` 调整：

```dotenv
FREE_CONSOLE_PORT=8866
FREE_CONSOLE_PUBLISH_PORT=18866
FREE_CONSOLE_BIND_ADDRESS=127.0.0.1
```

此时健康检查仍走容器内 `8866`，Nginx upstream 改为 `127.0.0.1:18866`。若不使用 Nginx，才将 `FREE_CONSOLE_BIND_ADDRESS` 显式改为 `0.0.0.0`。

容器内 Gunicorn 固定为一个 worker、多个 threads：任务进程句柄由单个服务进程管理，多线程负责 API 和日志轮询。账号执行并发由页面中的 `workers` 控制。

### Nginx

复制 `deploy/nginx.conf.example` 到服务器 Nginx 配置，替换域名、证书路径和 upstream 端口后启用。模板将 HTTP 跳转到 HTTPS；控制台自身的 `FREE_CONSOLE_PASSWORD` 继续保留。

## systemd 部署

```bash
sudo useradd --system --home /opt/free-register-console --shell /usr/sbin/nologin freeops
sudo install -d -o freeops -g freeops /opt/free-register-console
sudo rsync -a --chown=freeops:freeops ./ /opt/free-register-console/
cd /opt/free-register-console
sudo -u freeops -H python3 -m venv .venv
sudo -u freeops -H .venv/bin/pip install -r requirements.txt
sudo -u freeops -H cp .env.example .env
# 设置 .env 中的 FREE_CONSOLE_PASSWORD 和运行配置
sudo cp deploy/free-register-console.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now free-register-console
```

## 数据与备份

需要备份的目录只有：

```text
data/settings.json          # 运行配置（0600）
data/status_poll.json       # 自动轮询配置（0600）
data/accounts/              # Outlook / Relay 账号池（0600）
data/account_vault/         # 加密账号仓储
data/pool_state.db          # 备用池与已注册账号的非敏感状态
data/jobs/                  # 任务元数据
output/jobs/                # 完整日志
output/results/             # 每批独立 JSONL
output/core/                # success.txt、Agent Identity、导出文件等
output/health/              # 存活检测日志
```

任务输入和代理池写入 `data/inputs/`，权限为 `0600`，子进程结束后自动删除。异常重启时残留文件可在确认没有运行任务后清理。

## 浏览器模式说明

protocol 模式最适合 Linux 服务器。browser 模式还需要满足其一：

1. BitBrowser API 可从服务进程访问；
2. RoxyBrowser API 可从服务进程访问；
3. 主机安装 Chromium，并为服务进程提供图形会话。

Docker 默认安装 Chromium，但普通服务器部署仍建议优先使用 protocol。浏览器宿主地址可在“运行配置 → 浏览器宿主”修改。
