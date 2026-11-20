# 单主机部署与运维准备

2026-11-20：当前运行锁已移除 Chroma，Mac 192 / Linux 195 包，旧锁保存在 `dependencies/legacy-chroma-20261120/`。旧向量先用旧环境离线导出，再使用新环境或镜像的 `/app/scripts/import_vectors.py` 按摘要导入；命令见[迁移与回滚](VECTOR_MIGRATION.md)。不要直接启动新版去覆盖旧库。容器禁用 mount/umount/nsenter/unshare 并去掉 Set-ID；这不能代替 util-linux 包修复。系统扫描仍有命中，发布保持阻断。

本轮提供部署配置和可执行检查，**没有开放公网，也没有完成真实 OIDC、模型或域名验收**。Pydantic AI 整改后，当前 Mac 锁仍有 Chroma 的 4 组公告，部署入口会拒绝启动。CI 的镜像、TLS 和恢复检查通过也不能解除这个阻断。

## 结构

一个应用进程、一个 nginx TLS 入口，应用端口只在容器网络内使用。默认代理只发布到主机 `127.0.0.1:18443`，不会直接公开监听。未来确定域名、证书、主机防火墙和真实验收后，再单独修改公开入口。

应用以 UID/GID `10001:10001`、代理以 `101:101` 运行。两者只读根文件系统，去掉全部 capabilities，禁止提权，设置 CPU/内存/PID 和日志大小上限。只有数据目录可写；模型、配置与证书只读挂载。该配置限制资源和权限，不是执行目标代码的沙箱。

nginx 保留 WebSocket，限制每 IP 请求和连接数，设置 2 MiB HTTP 请求上限。CORS/XSRF 和 Streamlit 的 2 MiB 消息限制保持启用。入口覆盖 Host/转发协议和来源 IP，不信任用户自带的转发头。访问日志关闭，错误只记 critical，避免认证回调参数进入普通代理日志。真实回调和共享出口 IP 下的限流阈值仍需试用验证。

## 构建与审计

Linux CI 使用 Python 3.12.13、uv 0.12.3 与 Debian 13，直接复用最近成功 CI 的 `dependencies/runtime-linux-cpu.lock`（196 个运行包），核对 SHA256 及 193 个公共版本后按哈希安装，不在本轮底座升级时重新解析依赖。当前 Mac 锁为 `dependencies/runtime-macos.lock`，193 个版本；历史阶段零的 194 个版本不覆盖。`deploy/runtime.lock` 是该固定文件的构建副本和 CI 产物。

Dockerfile 要求传入已检查的 Python/uv 镜像摘要；nginx 和扫描器也记录摘要。CI 保存平台锁、镜像信息、OS 包清单、Python/OS 公告及烟雾检查。没有自动上传镜像或部署生产环境。构建上下文采用白名单，排除 `.git`、实际 `.env`、用户数据、测试数据和模型缓存。

Dockerfile 在 Debian 13 新镜像内应用已发布系统更新，检查 `apt`、`libapt-pkg7.0`、`liblzma5`、`libpcre2-8-0`、`libssl3t64`、`openssl`、`tzdata`、`libsqlite3-0`、`zlib1g` 的版本下限。`deploy/os-security-minimums.json` 绑定最近真实成功扫描，原生 `dpkg --compare-versions` 未达标直接构建失败。原 38 项作为回归检查保留，包更名单独记录，另外要求 SQLite 和 zlib 的两个严重公告消失；不能将 40 项检查都记成此次新增修复。完整系统扫描和严格发布阻断继续执行。

`deploy/storage-compatibility.json` 固定最近成功的源码、Bookworm 基础镜像、Linux 锁和 UV/nginx/Trivy 镜像摘要。CI 用该源码及同一锁构建基线镜像，在断网、UID 10001 的临时容器中写入测试 Git、向量和双用户会话；新 Trixie 镜像从同一固定路径直接回读，再独立进行停服备份恢复。检查完整向量、源码、原生消息内容和跨用户拒绝。此流程只使用临时样例，不迁移业务数据；真实迁移仍需停服备份、明确的挂载路径和实际主机验收。

正式推广必须使用对应的不可变应用镜像 digest 和 config image ID，并保存本次锁与审计产物。重新构建或换基础镜像后重新审计。当前 Dockerfile/Compose 不替代镜像签名、实际主机加固或真实公网验收。

## 配置准备

从 [environment.example](../deploy/environment.example) 建立被 Git 忽略的 `deploy/environment`。配置目录分别建立 `model.env`、`secrets.toml`、`access.toml`、`tasks.toml`、`audit.json` 和 `os-audit.json`。模型及 OIDC 值沿用[登录说明](AUTHENTICATION.md)，不在本轮填写真实密钥。

敏感配置必须归应用 UID 10001 所有、权限 `600`。nginx 配置是普通只读文件；证书私钥归代理 UID 101 所有并设为 `600`。不要将整份主机目录或 Docker socket 挂进应用。示例文件只含占位值，不能直接上线。

持久化目录分为 `chroma_db`、`repos`、`tasks`、`reviews`、`evaluations`，归 UID 10001 所有。它们分别挂到 `/app/chroma_db`、`/app/repos` 和 `/app/.baseline_runtime/` 对应目录。应用路径固定为 `/app`，保留原生记录内的绝对源码路径。

公开固定评测需要单独的只读 Git 检出，挂到 `/evaluation-repository` 并保留固定 Commit 对象和评测数据。`CODEBASE_QA_EVALUATION_REPOSITORY` 指向该目录；不把开发者工作区及其 `.git` 烘焙进镜像。评审运行应登记在 `/app/.baseline_runtime/evaluations/` 下，否则备份会拒绝遗漏归档。

嵌入模型仍是原 `all-MiniLM-L6-v2`，缓存清单锁定原 revision 和 12 个文件摘要。用已有可信缓存准备新的专用目录：

```bash
.venv/bin/python scripts/seed_embeddings.py /trusted/huggingface /new/model-cache
.venv/bin/python scripts/render_proxy.py qa.example.org --output /new/config/nginx.conf
```

缓存工具只复制清单里的公开模型文件和版本指针，不复制 HF token，不下载新模型。生产启动会核对缓存摘要。模型缓存不进入业务备份，应独立保留来源和校验清单。

配置齐备且漏洞整改完成后才能执行：

```bash
docker compose --env-file deploy/environment -f deploy/compose.yaml config --quiet
docker compose --env-file deploy/environment -f deploy/compose.yaml run --rm app --check
docker compose --env-file deploy/environment -f deploy/compose.yaml up -d
```

部署入口依次检查 Python/OS 审计、镜像绑定、真实 HTTPS 地址、OIDC 回调、配置权限、模型密钥、数据目录和模型缓存，未满足则在监听前退出。它不请求模型，也不验证真实登录往返；通过只说明配置检查通过。没有自动重启策略，防止错误配置或资源失败持续重启。

## 健康与维护

`/_stcore/health` 及代理 `/healthz` 只返回存活状态，不包含用户、问题、队列或配置。健康返回 200 不等于模型、OIDC 或数据恢复验收成功。

本机运维 CLI 才能查看任务总量、停止接单和恢复接单，不向普通页面增加管理接口：

```bash
docker compose --env-file deploy/environment -f deploy/compose.yaml exec app python scripts/manage_service.py status
docker compose --env-file deploy/environment -f deploy/compose.yaml exec app python scripts/manage_service.py drain --timeout 330
```

停止接单标记和接单使用同一文件锁。正在运行的任务继续完成，状态仍可查看；等待超时保持停止接单，不自动重跑或恢复。确认空闲后停止服务与代理。强制停止可能留下中断任务，下次启动不重放模型请求。维护后通过同一 CLI 的 `resume` 操作恢复接单。

## 停服备份与恢复

先完成 drain、停止应用及其他本机 CLI 写入，再备份。工具另取任务锁和运维锁，检查 SQLite 完整性、符号链接、文件变化、清单和大小；不支持在运行中拼接多个数据库快照。

原生工作区示例：

```bash
.venv/bin/python scripts/backup_state.py create --root /srv/application \
  --service-stopped --output /private/backups/state-new.zip
.venv/bin/python scripts/backup_state.py restore /private/backups/state-new.zip \
  --runtime-root /srv/application --output /private/restored-new
```

容器数据备份以 `/app` 为运行根，归档和恢复目录应额外挂到运维容器的独立目录，继续用 UID 10001。创建时只包含会话/向量、Git 快照、任务、评审及已登记评测；不包含 `.env`、OIDC 文件或模型缓存。归档权限 `600`，异地保存应使用加密存储，不能上传到公开仓库。

恢复只允许新目录，拒绝路径穿越、特殊文件、缺文件、大小/摘要不符和未知格式。恢复时的运行根必须与记录一致；先在新目录校验，再将数据挂回同一 `/app` 路径。工具不会改写历史来源或覆盖旧数据。配置、密钥、证书、公开评测 Git 与模型缓存独立恢复。

回滚按“停止接单 → 等待/记录中断 → 停服 → 备份 → 还原匹配镜像和数据 → 检查 → 恢复接单”执行，不只回滚代码。不要让不理解用户归属的旧版本接管多用户数据。备份可能包含 `draining` 标记，恢复核对完成后再主动 resume。

## 本轮验证边界

本机执行离线测试、真实临时 Git/Chroma/SQLite 的独立进程恢复演练和缓存核对。Linux CI 负责实际锁解析、镜像构建、非 root 导入、Compose 检查、用临时证书验证 TLS/WebSocket/限流和系统扫描。本机没有 Docker；本地 PyPI/注册表连接失败的原始日志保留，不能当作容器验证成功。

所有模型、OIDC 和 TLS 试验数据均为测试样例。真实模型成功基线、登录回调、生产证书、外网访问、主机备份恢复和负载验收仍在后续范围。当前已知依赖公告未整改，发布检查保持阻断。

实现参考：[Streamlit 容器说明](https://docs.streamlit.io/deploy/tutorials/docker)、[Docker 配置与凭据挂载](https://docs.docker.com/compose/how-tos/use-secrets/)。本项目使用只读文件挂载并自行检查权限，不声称普通 Compose 文件挂载自动加密。
