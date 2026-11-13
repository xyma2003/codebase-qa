# 登录与用户数据隔离

首发范围是受邀用户、公开 GitHub 仓库、只读分析和修改规划。当前完成登录接入及程序权限检查；真实 OIDC 往返、HTTPS Cookie 行为和聊天模型验收待配置密钥后进行。

## 两种启动方式

本地单人使用：

```bash
.venv/bin/python scripts/run_local.py --port 18524
```

该命令显式开启本地操作员，只绑定 `127.0.0.1`，保留原会话和缓存。此模式不能置于公网反向代理后。将本地模式改为监听非回环地址时，页面拒绝访问。

受邀用户模式：

```bash
.venv/bin/streamlit run app.py
```

默认要求 OIDC 登录。缺少配置、身份过期、未受邀、issuer 不符时拒绝业务访问，不退回匿名或本地操作员。公开部署的 HTTPS、代理和 Cookie 验收属于后续部署项，现在不应直接公开服务。

## 配置文件

以后配置登录时，分别从 `.streamlit/access.toml.example` 和 `.streamlit/secrets.toml.example` 建立同名实际文件，去掉 `.example`。实际文件已被 Git 忽略，建议权限 `600`。不要在聊天或 Git 中粘贴密钥。

- `secrets.toml`：OIDC 客户端、服务商元数据地址、回调 URL 及独立随机 Cookie 密钥，至少 32 字符。保持 `expose_tokens = []`，不向应用暴露身份/访问令牌。
- `access.toml`：一个固定 issuer，以及受邀用户的准确 subject 和角色。角色仅 `user`、`admin`。用户编号由 issuer+subject 计算，不从邮箱或模型输入推断角色。subject 应从身份服务商的可信管理渠道取得。
- 邀请名单每次请求和私有服务访问重新检查。撤销邀请或变更角色后，正在运行的请求也不能继续保存私有结果。检查 `exp`、`iat` 及可选 `nbf`；过期需要重新登录，不依赖 Streamlit Cookie 的默认保存时间。
- Cookie、客户端密钥和模型密钥分别管理。真实 OIDC 配置后，须检查签名/issuer/audience 的服务商接入、回调失败、退出、Cookie 属性及真实浏览器换号行为；当前契约测试不替代这些验收。
- 加载登录配置时，客户端密钥及 Cookie 密钥加入原有传递拒绝与日志脱敏规则；不从目标仓库读取登录配置，不在错误中打印密钥。运行期保留最多 64 个配置凭据值用于脱敏，频繁轮换后需重启进程。

实现使用现有 Streamlit 1.67.0、Authlib 1.9.5；Authlib 补为直接依赖，没有安装或升级包。实现依据：[Streamlit OIDC 说明](https://docs.streamlit.io/develop/concepts/connections/authentication)、[st.login](https://docs.streamlit.io/develop/api-reference/user/st.login)、[st.user 的过期检查说明](https://docs.streamlit.io/develop/api-reference/user/st.user)。

## 数据权限

| 内容 | 权限 |
| --- | --- |
| 公开源码快照与代码向量 | 已登录用户可以复用，仍检查公开仓库、固定版本和源码策略 |
| 问题、回答、原生会话历史 | 仅创建者可列出、读取、追加；管理员不会自动绕过归属 |
| 历史答案缓存 | 用户独立集合，只有本人可查看、批准、撤销与检索；不作为共享知识库 |
| 评测运行和评审材料 | 仅管理员可管理；同时受运行登记及用户归属限制 |
| 本地旧数据 | 保留为 `local` 操作员，不自动分配给新登录用户 |
| 本地 MCP / CLI | 仍属于可信本机操作员，不作为远程浏览器身份代理 |

页面和服务分别检查权限，工具没有用户编号或角色参数。会话 SQLite 添加 `owner_id`，查询与条件更新同时限定所有者；消息和旧行不删除。缓存新命名空间绑定用户及来源，不重建公开代码索引。身份通过 ContextVar 传入同一轮的异步工具调用，不使用进程全局“当前用户”。

退出或换号清空页面状态；缓存批准回调在执行时重新检查登录，因为 Streamlit 回调先于页面正文运行。运行过程中撤销访问或身份过期，已完成模型结果也不进入会话。主机管理员及能在服务器执行 Python 的调用方仍属于信任边界。

## 管理员评审

在线评测页不提供服务器目录输入，只选择已登记且属于自己的运行。原本地操作员页面保留目录入口。登记只能由可信本机 CLI 完成：

```bash
.venv/bin/python scripts/register_review_run.py /absolute/path/to/sealed-run \
  --name "评测运行" --issuer https://identity.example.invalid --subject your-admin-subject
```

命令输出运行编号。登记位于 `.baseline_runtime/reviews/catalog.json`，记录归档封存摘要；网页不能自行登记路径。在线评审包和报告位于 `.baseline_runtime/reviews/users/<用户编号>/`。打开、保存、报告生成和下载均核对权限及路径，猜测其他人的编号无效。

## 验证与回滚

`tests/test_user_access.py` 使用本地假身份声明、真实临时 SQLite/Chroma、真实 SDK FunctionModel 和 Streamlit AppTest，检查双用户隔离、撤销、过期、直接打开管理页、回调和下载越权。没有伪造真实身份服务商成功记录，也不请求聊天模型。

回滚登录功能前应先停止受邀用户服务，并保留当前数据库和用户缓存。旧版本不理解用户归属，不能直接让旧程序接管已有多用户数据；应恢复对应的本地数据版本。当前没有自动删除或迁移旧会话、缓存和代码索引。

后台任务、并发及每日配额、容器隔离、依赖漏洞审计、备份恢复演练和公网验收仍待后续实施。

登录提交后的 CI 发现旧页面全局 `nest_asyncio` 补丁使测试结束后留下 AnyIO 线程。已独立修复：页面模型工作在稳定事件循环中执行，ContextVar 按请求传入，工具事件只在原页面线程展示。新增生命周期及双用户线程检查，全量 328 项通过且进程退出成功。原超时记录和对照堆栈保留在[修复记录](checks/20261113-ui-runtime.md)。
