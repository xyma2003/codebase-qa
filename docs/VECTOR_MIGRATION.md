# 本地向量库迁移

运行存储改为 `chroma_db/vectors.sqlite3`，沿用原数据目录名。SQLite 只保存文本、JSON 和 float32 数组；NumPy 计算精确距离。原分块、all-MiniLM-L6-v2 CPU 非归一化嵌入、集合名、源码版本、审核缓存及会话绑定保留。旧集合使用 cosine；导入时逐集合保留 cosine / l2 / ip。精确扫描上限为每集合 10000 条、4096 维、64 MiB，不能当作大规模 ANN 服务。

## 迁移

先停止网页、任务工作进程和所有 CLI 写入，保存原数据目录与原环境。导出只使用原环境的 Chroma，读取集合时明确禁用自动嵌入。导出的中间文件包含源码、元数据和可能的私有问答，文件权限为 600，应留在私有备份目录，不提交或上传。

```sh
ARCHIVED_PYTHON scripts/export_legacy_vectors.py --source /state/chroma_db --output /private-backup/vectors.json --service-stopped
NEW_PYTHON scripts/import_vectors.py /private-backup/vectors.json --destination /state/chroma_db --sha256 EXPORT_SHA256 --service-stopped
```

`ARCHIVED_PYTHON`、`NEW_PYTHON` 和 `EXPORT_SHA256` 分别替换为两个环境的解释器及导出命令返回的 SHA256。导入不需要安装 Chroma，不调用模型或重算嵌入。CLI 持维护锁，导入校验文件摘要、格式、metric、原嵌入配置、唯一 ID、维度和数值；临时库完整回读与 integrity_check 通过后才原子发布。已存在新库时拒绝覆盖，失败只清理本次临时文件。单个导出最多 256 MiB，导入只接受固定原嵌入配置，不执行持久化配置里的类或模型。

出现 `chroma.sqlite3` 而没有新库时，应用拒绝自动创建空库，要求先迁移。迁移不修改 `repository_state.sqlite3`、`conversations.sqlite3` 或原 Chroma 文件；重启前需要核对就绪仓库、缓存审核、用户归属、检索和恢复。升级环境与历史依赖锁分别保存，不能用当前锁反向安装旧环境。

## 检索对照与回滚

使用旧环境在保留的旧库上运行 `scripts/check_vector_migration.py --legacy --source OLD_COPY --output REFERENCE`；然后用新环境运行同脚本，传 `--source NEW_STORAGE --reference REFERENCE --output REPORT`。固定 19 个问题来自版本库，不修改原评测题或模型参数。参考文件包含本地查询向量，应私有保存。顺序或距离超出阈值会失败并保留对照；精确检索与近似检索可能有差异，不能靠放宽阈值掩盖结果。

本机 4 集合 / 1256 条向量已迁移且逐条完全相等，76 次 top-5 的结果与顺序一致，嵌入误差 0，距离最大误差 3.424e-7。原 2 会话 / 3 轮内容摘要一致，详见[本轮验收](checks/20261120-sqlite-vectors.md)。这些结果不能证明未测数据规模的性能或真实模型问答质量。

回滚前先停止所有写入并保留新库与新写入备份；切回原代码、原环境和升级前完整数据副本。**旧版代码不会读取升级后写入新库的记录**，不能只回退 Git 代码后继续混用两个库。新版本保留旧 Chroma 文件用于回滚，不会同步双写。容器 CI 用临时数据验证旧 Chroma → 新 SQLite → 新版备份恢复，真实服务器迁移仍需独立验收。
