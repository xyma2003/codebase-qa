# 人工评审导入与评分报告

这组命令把填写好的评审记录绑定到已有运行，再生成整体、分类和逐题报告。原始运行、评审工作包、导入后的报告分别保存，不回写已经封存的回答。

当前支持的源档案仍是本地模拟输出。已发布的[报告样例](../docs/checks/20261111-review-import-sample.md)使用明确标记的合成评分，只验证导入和算分流程；实际人工语义评审和真实模型评测尚未进行。

## 页面填写

在现有 Streamlit 应用侧栏进入“评测评审”。填写已封存的运行归档目录，选择“新建评审”或已有评审记录，点击“打开评审”。例如本机已保存的归档目录为 `.baseline_runtime/reviews/20261111-import-check/archive`；目录相对于项目根目录，也可填绝对路径。

页面按题目与轮次选择，左侧显示问题、此前对话和原回答，右侧显示核对要点及带固定版本链接的源码。展开“评分规则”后，填写四项分数、评审者和理由；有致命错误时选对应标记，再点击“保存本轮”。页面不自动打分，未保存的表单不计入报告。

评审包保存于 `.baseline_runtime/reviews/ui/packets/`。重新打开页面后，填写相同归档目录，选择对应评审记录即可继续。已保存记录可修改，或点击“清除本轮评分”恢复待评审。两个页面同时编辑时，旧页面会拒绝覆盖新记录；重新打开即可读取新值。CLI 创建的外部工作包仍可按下面的命令编辑和导入，页面选择器列出本页面工作目录里的记录。

点击“生成报告”会在 `.baseline_runtime/reviews/ui/reports/` 创建新报告，不覆盖旧报告。支持下载 Markdown 和完整 ZIP；ZIP 保留封存清单，可解压后用下面的 `verify` 命令和原归档独立核对。有未运行或未评审项时，最终均分和通过率保持待评审。流程样例、测试记录见[界面检查记录](../docs/checks/20261112-review-ui.md)。

界面和 CLI 共用同一套评审记录、输出 SHA256 绑定、评分公式及报告格式。当前界面同样只接收本地模拟归档，真实模型验收待密钥配置后推进。

## 准备与填写

在项目根目录的终端运行，`--archive` 指向已经通过校验的归档目录：

```bash
.venv/bin/python scripts/review_evaluation.py prepare --archive .baseline_runtime/evaluations/manual-local-001 --output .baseline_runtime/reviews/manual-packet-001
```

命令默认 `--purpose human`。只有构造测试评分时才使用 `--purpose simulation`。包内有四个文件：

| 文件 | 操作 |
| --- | --- |
| `context.json` | 查看各题完整多轮问题、原回答、引用、参考检查点及执行状态。 |
| `rubric.json` | 查看固定四维规则和致命错误定义，不修改。 |
| `review.json` | 绑定运行编号、归档 SHA256、数据集和规则摘要，不修改来源字段。 |
| `reviews.jsonl` | 填写已经完成输出的轮次；题号、轮次号及输出 SHA256 保持原值。 |

逐题先核对完整上下文，再核对源码依据，按规则填写正确性、证据、完整性和边界说明四项。程序不根据关键词、回答长度或引用格式代填分数，也不替评审者作语义判断。

一条完成评审的记录应包含 `status="reviewed"`、四个 0/1/2 评分、评审者标识及具体理由。以下只展示需编辑的字段，数值须由实际评审决定：

```json
{
  "status": "reviewed",
  "ratings": {"correctness": 0, "evidence": 0, "completeness": 0, "boundaries": 0},
  "fatal_errors": [],
  "reviewer": "reviewer_name",
  "rationale": "说明哪些检查点满足或缺失，以及对应依据"
}
```

`fatal_errors` 只能填固定规则已有的代码，如 `wrong_source`、`fabricated_evidence`、`false_execution`、`cross_session_context`。其含义和证据要求见固定判分规则；不能把合成测试标签当作真实错误结论。

允许保留尚未评审的行，也可只提交本次已填的部分记录；缺少的轮次保留原状态和固定分母。失败、阻塞、未运行项不能改成已评审回答，原执行错误也不能改写。

## 导入并查看结果

```bash
.venv/bin/python scripts/review_evaluation.py import --archive .baseline_runtime/evaluations/manual-local-001 --packet .baseline_runtime/reviews/manual-packet-001 --output .baseline_runtime/reviews/manual-report-001
.venv/bin/python scripts/review_evaluation.py verify --archive .baseline_runtime/evaluations/manual-local-001 --report .baseline_runtime/reviews/manual-report-001
```

导入先核对源档案、规则、评审上下文及每轮输出 SHA256，再计算报告。重复题号、未知轮次、错运行、错输出、修改过的规则或参考标注都会拒绝。已有目录不覆盖，报告不能写入源档案或评审工作包内部。

报告包含：

- `report.md`：整体与四类结果，已标记的问题、完整问题上下文、原回答、版本化源码链接和评语。
- `report.json`：完整结构化结果及评分来源、运行/数据/规则绑定、评分代码版本与文件摘要。
- `badcases.jsonl`：至少有一轮明确失败的题目材料；即使另一轮仍待评审，已知问题也不会被隐藏。
- `merged-reviews.jsonl`：合并后覆盖固定分母的全部轮次，未提交项不会消失。
- `submission/`：本次导入的原始记录、上下文和规则副本，保留修订依据。
- `report-seal.json`：最后写入的文件清单和 SHA256；独立回读还会重算报告并核对渲染结果。

一题需所有轮次通过才通过，题内各轮平均后对固定题目等权平均。有未运行或待评审项时，整体和分类最终均分/通过率均为未知；当前评审进度和已知问题仍可查看。权重、通过条件和致命错误规则沿用[固定判分规则](../evals/codebase-qa-v1/README.md)，本次没有调分。

填写后需要修订时，可修改工作包并导入到新的报告目录。旧报告保存的原始提交和输出绑定不会随工作包变化。不同评审者或不同轮次修订也应保留独立记录，不能直接覆盖源运行中的 `reviews.jsonl`。

## 复核已保存的本地样例

样例复用上次的成功归档，没有重新调用模型。原归档中的 19 轮仍保持待评审；演示评分保存在另一份报告中，来源为 `synthetic_fixture`，不产生真实模型质量结论。

把两个已经保存的包解压到新的目录，再执行回读：

```bash
.venv/bin/python -m zipfile -e docs/checks/20261111-evaluation-runner-local.zip .baseline_runtime/reviews/sample-source
.venv/bin/python -m zipfile -e docs/checks/20261111-review-import-local.zip .baseline_runtime/reviews/sample-report
.venv/bin/python scripts/review_evaluation.py verify --archive .baseline_runtime/reviews/sample-source --report .baseline_runtime/reviews/sample-report
```

回读需要原始归档和对应 Git 对象，不需要原来的评审工作包、运行进程或 SQLite 工作状态。报告中的分数是记录按固定规则的计算结果，不能替代对评审理由的人工复核。
