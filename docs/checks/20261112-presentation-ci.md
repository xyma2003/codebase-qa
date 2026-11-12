# 首页、演示和持续检查

日期：2026-11-12。

首页增加工程能力与可检查结果的对应关系、真实页面截图、架构与演示入口。架构说明列明各服务边界；演示可复用已有归档并展示源码、历史和评审，不需要聊天密钥。固定检索实验中的未提升结果原样保留，没有准确率或成本宣传。

## 本地检查

- 全量 252 项测试通过，83.574 秒，见[测试日志](20261112-presentation-ci-unittest.log)。新增篡改检查证明：即使重绑输出摘要并重封存档案，伪造的运行请求数仍会被拒绝。
- 原依赖环境经 `uv pip check` 检查兼容；本机 uv 虚拟环境未安装 pip，没有为检查而修改它。CI 的 setup-python 环境使用其自带 pip 和原哈希锁安装。
- 固定 16 题、19 轮和 31 段证据再次核对，模板仍为未运行，质量分数为空。
- 解压已发布的接口模拟 ZIP，在独立进程回读通过：19 轮完成，0 轮人工评分，0 次真实聊天调用。原归档摘要 `cd67b59e8ca4f632d8d7b817b91354a96bb14323aef37be9ab2b549456dce2f8` 未改变。
- 文档链接、工作流配置、空密钥、完整 Git 历史、固定 Actions Commit 和显式 bash / pipefail 已检查。新运行输出的设置与用量交叉核对；评审页面的样例与真实指标标签按来源区分。

## 远端检查入口

[CI 工作流](../../.github/workflows/ci.yml)在 main 推送和 PR 时运行，也可手动触发。安装原锁定依赖后，执行固定数据核对、全量测试及旧归档回读；测试使用本地模型和模拟接口，Hugging Face 离线，不配置聊天密钥。检查文件保存在 runner 临时目录并上传为 `offline-check-records`，保留 14 天。

Actions 的版本和 Node 24 配置已从官方仓库核对：[checkout v7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1)、[setup-python v7.1.0](https://github.com/actions/setup-python/releases/tag/v7.1.0)、[upload-artifact v7.0.2](https://github.com/actions/upload-artifact/releases/tag/v7.0.2)。[官方 runner 清单](https://github.com/actions/runner-images#available-images)中的 `macos-15` 为 arm64，工作流固定 Python 3.12.13，与本机平台一致。完整 Git 历史用于原版与固定集源码核对。

本记录描述本地验收和已配置流程。每次推送的真实远端状态、失败步骤与日志以 [GitHub Actions](https://github.com/xyma2003/codebase-qa/actions/workflows/ci.yml) 对应 Commit 为准，不把本地通过写成远端已通过。真实模型质量仍待成功基线、密钥与内容评审。

## 首次远端校验与修正

首次运行 [43153385471](https://github.com/xyma2003/codebase-qa/actions/runs/43153385471) 未创建作业。GitHub 原始错误为 `(Line: 25, Col: 16): Unrecognized named-value: runner`，指向作业级环境中的 `runner.temp`。现将唯一相关字段 `HF_HOME` 移到支持该上下文的测试步骤环境，未改应用或模型配置。下载并核对官方发布摘要后，使用 actionlint 1.7.12 检查通过，退出码 0；原始错误、修正范围和校验摘要保存在[记录](20261112-ci-correction.json)。这补齐了此前通用 YAML 检查没有覆盖的 Actions 上下文规则。
