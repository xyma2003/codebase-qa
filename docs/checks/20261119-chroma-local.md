# Chroma 本地配置边界 · 2026-11-19

最终全量 **387 项通过，123.546 秒，进程退出 0**，新增 4 项边界检查。[日志](20261119-chroma-unittest.log)、[实现与数据指纹](20261119-chroma.json)及[最新公告元数据](20261119-chroma-audit.json)分别保存。

## 变更

源码索引与私有缓存统一使用 `security.vector_client.local_chroma_client`。客户端固定 Rust 嵌入式 API，组件配置不读取进程环境、`.env` 或 secrets 目录；禁用 reset、匿名遥测及 OTel 导出。没有开放 Chroma REST，没有接入其服务端认证或 RBAC。

测试先向环境、dotenv 和 secrets 注入远程 API、任意组件路径、服务器及遥测地址和 reset 开关，再用真实 Chroma 创建本地集合，写入显式测试向量并查询。结果保持本地，reset 被库拒绝；另一个独立进程从带有恶意环境的首次导入开始验证没有加载指定组件。源码与缓存两个入口均核对共用此边界。

首次测试将 reset 拒绝异常误写为 ValueError；实际库返回 AuthorizationError，原错误保留并修正断言，四项定向检查及最终全量通过。没有修改实际拒绝逻辑来迎合测试。

## 回读与限制

实际 MCP stdio 的 9 个工具、14 次调用通过，活动源码仍为 `7ed1f8f22e3c1caf1c23b9ca976bf9e86a5feffb`、58 分块。独立进程的临时 Git/Chroma/双用户原生消息备份恢复通过。旧业务会话仍为 2 个、3 轮，内容摘要 `9fd14008178680c699d441cd6e83dd0056b35b1f4c0faf206df92afec7de647f` 保留。未重建索引，模型、嵌入和全部 193 个包版本不变。

当前完整 OSV 复查仍有 8 个编号、4 组 Chroma 公告。[官方稳定发行版](https://github.com/chroma-core/chroma/releases/tag/1.5.9)仍为 1.5.9，两个代码注入公告 [GHSA-36p7-vc44-83pf](https://github.com/advisories/GHSA-36p7-vc44-83pf)、[GHSA-f4j7-r4q5-qw2c](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c)仍未给出 patched version。**本项是应用配置加固，不是上游漏洞修复；发布继续阻断。** 没有删除公告、使用忽略名单，或用开发版版本号避开扫描。

真实模型、OIDC 和公网验收后置。本文生成时尚未推送，远端 Mac/Linux 结果以本提交的 Actions 为准。
