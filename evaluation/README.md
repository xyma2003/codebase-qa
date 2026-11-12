# 评测运行与归档

默认运行器使用本地 `FunctionModel`，同时提供显式的 SiliconFlow 真实评测入口。真实入口要求原版成功配置、固定版本的实际向量索引及密钥，缺失时不请求模型。固定开发集的 16 题、19 轮本地流程已跑通，归档可独立回读。[评审导入与评分报告](REVIEWING.md)支持这两类输出；真实模型成绩仍待密钥、成功基线及实际人工评审。

模拟模型不读参考标注。它从模型收到的工具结果中选择源码、调用读取，再返回明确标注的演示回答或规划，不按题号预置标准答案。每个真实源码引用仍经过现有检查器核对。

## 在哪里运行

在项目根目录的终端执行：

```bash
.venv/bin/python scripts/run_evaluation.py --backend local --output .baseline_runtime/evaluations/manual-local-001
.venv/bin/python scripts/verify_evaluation_archive.py .baseline_runtime/evaluations/manual-local-001
```

默认本地运行不需要密钥，不调用模型 API。省略 `--output` 会生成带时间和随机编号的新目录；已有目录一律拒绝覆盖，包括失败或未完成目录。真实后端必须显式选择并提供成功基线和固定索引，没有自动切换模型、补跑或续跑入口。

运行器按固定问题顺序逐题执行，每题新建会话，同题内按 T1、T2 顺序继续。任何一轮失败，记录原始错误并停止本次运行；该题后续轮次标为 `blocked`，其他尚未开始的题标为 `not_run`。正常工具循环会有多次框架模型步骤，运行器不重试失败轮次，不改参数重跑。

## 本地运行的隔离范围

每次运行从固定 Git Commit 导出独立快照，不使用脏工作区，不抓取远端，也不执行目标源码。会话库保存到该运行目录中；`source_only=True` 禁用问答缓存读写、向量搜索和已索引文件列表，实际使用字面定位、源码读取等不依赖向量的工具。

默认 `local` 后端使用仅源码的模拟环境，不使用真实向量索引。会话需要的集合字段使用明确标注的占位符，清单的 `index.status` 为 `not_built`，不得把它当作真实 ChromaDB 集合或检索验收结果。独立的接口模拟检查已为 `af7dcd1` 建立并回读 533 个真实向量，供版本 2 运行使用；两类运行不混用来源。正式应用的默认工具和缓存行为保留。

## 归档文件

| 路径 | 内容 |
| --- | --- |
| `started.json`、`run.json` | 开始配置及最终状态，含数据集指纹、目标源版本、执行代码版本/文件摘要、Python/依赖、模型和本地运行设置。 |
| `dataset/` | 固定数据集与清单的原样副本，参考标注不传入模型。 |
| `inputs.json` | 题号、源身份、各轮问题与模式；没有类别和参考答案。 |
| `turns/001-01/input.json` | 第一题第一轮的准确问题、模式、来源及会话编号。目录使用数字编号，不用题目文本作路径。 |
| `requests.json` | 本轮每次本地模型实际收到的原生消息、指令、模型设置及工具定义；按会话核对问题序列。 |
| `messages.json`、`new_messages.json` | SDK 的完整原生消息，以及成功生成时的本轮新消息。 |
| `trace.json` | 从完整原生消息派生的工具调用、参数和返回值，范围为会话截至本轮，不能把历史调用重复统计成本轮调用。 |
| `native_output.json`、`output.json` | SDK 原始回答/规划，以及通过产品检查流程后的 `CheckedAnswer`。完成生成但检查失败时可能只保留原始结果，不发布完成输出。 |
| `metrics.json` | 本轮墙钟耗时、实际本地模型步骤数和框架估算用量。真实 provider 用量和费用为未知 `null`。 |
| `error.json`、`outcome.json` | 原始异常类型、消息、堆栈或未执行原因，以及本轮状态和尝试次数。 |
| `progress.jsonl` | 追加式逐轮进度，运行中断时保留已经记录的状态。 |
| `reviews.jsonl`、`summary.json` | 成功输出绑定到文件 SHA256，状态为待人工评审；失败记录原始原因，未运行仍保留固定分母。没有人工评分时不发布均分或通过率。 |
| `archive.json` | 输入输出文件清单和 SHA256，最后才写入；缺少该文件时不能算完成归档。 |
| `_state/` | 独立源码快照和 SQLite 工作状态，不进入便携归档，不是独立校验器信任的结果来源。 |

若写盘失败或进程中断，可能留下部分输入、消息和进度，但不会生成有效完成标记。不要覆盖旧目录来补跑，后续明确重跑时使用新目录，保留原始错误和配置。

运行时代码尚未提交时，清单如实记录 `runtime_dirty=true`，同时保存运行器列出的实际运行文件 SHA256。不能仅凭 `runtime_commit` 就把未提交修改说成该 Commit 的原始代码；判断本地样例实际代码应结合文件摘要。成功输出另外保存逐轮运行设置与 SDK 用量；旧归档缺少该字段仍可回读。

## 独立回读

校验器从归档重新加载固定数据集，核对所有文件摘要、题目与源身份、固定轮次、跨题会话隔离、多轮原生问题、工具轨迹、输出/评审摘要绑定及源码引用。原始回答与最终模型响应、原始规划与结构化输出工具调用也会核对。它还检查遇错停止的顺序，防止后续未执行轮次被写成完成。

回读不需要原进程、可写 SQLite 或快照工作目录；源码依据直接对固定 Git 对象复核。没有该 Git 对象的浅克隆不能冒充已完成来源核对。当前回读验证本地运行和档案一致性，不进行语义评分或真实模型效果评估。

已保存的[便携样例](../docs/checks/20261111-evaluation-runner-local.zip)可解压到新目录后检查：

```bash
.venv/bin/python -m zipfile -e docs/checks/20261111-evaluation-runner-local.zip .baseline_runtime/evaluations/unpacked-local-sample
.venv/bin/python scripts/verify_evaluation_archive.py .baseline_runtime/evaluations/unpacked-local-sample
```

归档中的评审文件和空模板保持封存。现在可以在独立目录生成评审工作包、导入填写的记录并核对原输出 SHA256，再生成报告；操作见[人工评审导入与报告](REVIEWING.md)。

## 后续真实评测

本地样例未使用真实 provider，其 token 数是框架估算，本地耗时只用于流程诊断，不是线上性能或费用数据。已有生产模型、接口、嵌入与质量阈值未更换。

补齐密钥后，先在阶段零检查点 `3aee328` 的独立目录执行原版 `inspect`、`index` 和 `ask`。三项全部通过，并保存 `answer.txt`、`model-messages.json` 和 `requirements-resolved.txt`，才可作为首次成功配置。当前保存的 `20261110-stage0/ask.json` 是阻塞记录，真实入口会拒绝它，不能用模拟结果替代。

在本项目根目录，显式准备固定集对应的索引；此命令只读本地固定 Git 对象，不抓取远端，不更换分块或嵌入参数，也不改变问答页面的当前版本：

```bash
.venv/bin/python scripts/build_evaluation_indexes.py --output .baseline_runtime/evaluation-indexes/real-001
```

索引使用新集合，清单记录完整代码版本、原文、行号、向量摘要及配置指纹。运行前逐条回读实际存储；清单、源码或向量不匹配就停止。旧索引保留。准备成功后，再显式调用：

```bash
.venv/bin/python scripts/run_evaluation.py --backend siliconflow --baseline /absolute/path/to/original-success-run --indexes .baseline_runtime/evaluation-indexes/real-001 --output .baseline_runtime/evaluations/real-001
```

入口核对原版源码摘要、成功输入输出、完整有效配置和依赖。配置不同会列出字段及依赖差异，不修改参数。接口沿用 SiliconFlow `Qwen/Qwen3-32B`；温度等未显式设置项保持原默认值，SDK 的原有内部重试设置也保留并记录。评测器本身不重新执行失败轮次，遇错停止，超时沿用本地运行器的 30 秒。输入、逐次 SDK 请求与响应、工具记录、原始错误、用量和配置分别存档。

真实评测使用专用索引和隔离会话，关闭历史问答缓存读写，防止固定题被旧答案污染；这些运行范围变化明确保存在配置记录里，应用默认缓存行为保持原值。用量来自实际响应，费用未核对时保持空值，不把框架估算当作账单。

版本 2 归档保存成功基线原始材料和索引清单，原 CLI 可以独立回读，评分与评审页面根据来源区分本地模拟、接口模拟和真实模型。真实运行完成仍不等于答案正确；正式成绩继续使用固定规则，由人核对内容并填写评分。

本地接口检查使用 `httpx.MockTransport`，不发送网络请求；日志中 SDK 显示的 SiliconFlow 地址也是模拟传输。检查使用真实 SDK、真实本地嵌入和向量存储，构造的基线仅用于检查前置条件，所有输出标记为 `provider_fixture`。见[检查记录](../docs/checks/20261112-provider-evaluation.md)。
