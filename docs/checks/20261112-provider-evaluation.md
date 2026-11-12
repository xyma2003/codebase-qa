# 真实评测接入的本地检查

日期：2026-11-12。范围：真实模型入口、索引、请求响应归档和报告衔接。此次没有发送付费模型请求，模型效果未评审。

## 本轮实现

- 显式 `--backend siliconflow`，要求提供原版成功基线与固定索引；默认仍为本地源码模拟。
- 核对原版 `inspect/index/ask` 成功材料、源码摘要、完整配置和依赖；缺密钥、字段漂移或原版问答阻塞时拒绝运行，不自动改参数。
- 从固定 Git 对象准备专用索引，回读原文、位置和向量摘要；不抓取远端，不发布为问答页面当前版本，不删除旧集合。
- 复用实际 Agent、源码工具、原生多轮会话和结构化规划。固定评测关闭历史答案缓存读写；应用默认缓存行为保留。
- 保存逐次 SDK 输入、工具与输出定义、原始响应、响应用量、错误及原生消息。密钥脱敏，费用未知时为空。
- 版本 2 档案绑定原版基线和实际索引；独立校验后接入评审与报告，来源分别为本地模拟、接口模拟和真实模型。

## 检查结果

全量 220 项测试通过，85.375 秒，退出码 0，见[原始输出](20261112-provider-evaluation-unittest.log)。其中新增 10 项覆盖实际 SDK 与临时 ChromaDB、失败停止、密钥脱敏、配置漂移、阻塞基线、源码摘要、向量损坏、输入隔离、模拟接口来源、原生多轮、规划、打包及新进程回读。

固定 16 题、19 轮使用已锁定的 `af7dcd1` 源版本，原有 `all-MiniLM-L6-v2` 本地嵌入及原分块参数，建立 533 条专用向量。真实 SDK 通过 `httpx.MockTransport` 完成 38 次模拟请求；传输没有访问 SiliconFlow。SDK 日志中的网址是模拟请求地址，不能据此当成真实调用。

前置校验使用明确的合成基线材料，原始问答内容写明未调用真实模型。该材料只能说明校验和运行流程可测，不能作为后续真实跑批的成功基线。归档标记 `backend=provider_fixture`、`real_model_called=false`。

19 轮输出均保持待人工评审，最终均分与通过率为空。报告为“接口模拟输出的人工评审报告”，没有预填评分。原版保存的 `ask.json` 仍为阻塞状态，正式真实基线仍待密钥。

运行和报告均通过新 Python 进程回读，问答页面活动索引前后一致。结果和源码摘要见[JSON 记录](20261112-provider-evaluation.json)，便携运行见[ZIP](20261112-provider-evaluation-local.zip)。新的专用集合只供本次评测，旧集合保留。

## 运行规则

评测器逐轮遇错停止，不自动重跑失败轮次；SDK 原有内部重试策略保持原值，并在档案记录 `sdk_max_retries`，不把一个 SDK 调用等同于一次底层 HTTP 请求。超时沿用已有运行器的 30 秒。真实运行前如需改动这些设置，先对照最近真实成功配置并明确字段差异。

复核便携归档时，在项目根目录解压到新目录：

```bash
.venv/bin/python -m zipfile -e docs/checks/20261112-provider-evaluation-local.zip .baseline_runtime/evaluations/provider-recheck
.venv/bin/python scripts/verify_evaluation_archive.py .baseline_runtime/evaluations/provider-recheck
```

回读需要对应 Git 对象，不需要原来的 ChromaDB、SQLite、评审包或模型密钥。准备真实入口的完整步骤见[运行说明](../../evaluation/README.md)。
