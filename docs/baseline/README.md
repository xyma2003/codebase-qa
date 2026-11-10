# 第 0 阶段运行基线

开始日期：2026-11-10。代码起点：`7ed1f8f22e3c1caf1c23b9ca976bf9e86a5feffb`。

用户确认此项目以前尚未成功运行，本次建立首个可重复的真实成功基线。阶段记录区分环境安装、界面启动、索引检索和真实模型问答，后续补入实际结果。

密钥、运行缓存和本机数据不进入版本库。基线记录仅保存脱敏配置、固定输入、结果、依赖版本及原始错误。

当前状态：环境、索引、MCP 工具和页面验证通过；真实模型问答待 SiliconFlow 密钥，完整端到端基线尚未建立。

本次实际结果见 [2026 年 11 月 10 日运行记录](runs/20261110-stage0/REPORT.md)。

## 固定输入

目标仓库为本项目的上述代码版本。[问题集](questions.json)固定了 6 个问题及人工核对点；本次模型链路复现先使用 B01。参考文件和核对点不注入模型提示词。

## 执行方法

在项目根目录使用独立虚拟环境运行。以下环境变量只指定运行缓存位置，应用模型和检索参数不覆盖。

```bash
export HF_HOME="$PWD/.baseline_runtime/huggingface"
export BASELINE_OUTPUT="docs/baseline/runs/20261110-stage0"
.venv/bin/python scripts/run_baseline.py inspect --output "$BASELINE_OUTPUT"
.venv/bin/python scripts/run_baseline.py index --output "$BASELINE_OUTPUT"
.venv/bin/python scripts/run_baseline.py mcp --output "$BASELINE_OUTPUT"
.venv/bin/python scripts/run_baseline.py ui --output "$BASELINE_OUTPUT"
.venv/bin/python scripts/run_baseline.py ask --output "$BASELINE_OUTPUT"
```

`inspect` 保存实际依赖、脱敏有效配置及原应用文件校验值。`index` 调用原有克隆、分块、嵌入和检索实现，并回读持久化数量。`mcp` 通过真实 stdio 协议检查 4 个工具。`ui` 使用 Streamlit AppTest 验证页面和已有索引状态。`ask` 调用原有 `ask_stream`，保存回答及工具消息。

补齐密钥后的新运行应使用新的输出目录，保留首次记录。目标代码目录必须仍为固定 SHA；如果以后远端更新，应先准备对应版本的目标目录，脚本不会自动换输入继续运行。

原版代码和脚本已保存在已推送的 `3aee328` 检查点。当前分支继续开发后，须从该检查点检出独立工作区，再完成原版模型问答；不要直接在修改后的应用上补写原版成功结论。

重建已测环境时使用记录中的完整依赖锁，而不是再次解析未固定版本的依赖：

```bash
uv pip install --python .venv/bin/python --require-hashes -r docs/baseline/runs/20261110-stage0/requirements-hashed.txt
```

本机已验证的下载通路为优先 `https://pypi.tuna.tsinghua.edu.cn/simple`、缺失版本回 `https://pypi.org/simple`；启用 `--require-hashes`。具体安装过程见运行记录。

每个阶段单独保存状态、耗时及原始错误。成功的索引不重复构建。目标版本不符时停止，避免使用变动后的代码冒充同一输入。

原应用在没有 `OPENAI_API_KEY` 时会选择 Anthropic 分支。本次已选 SiliconFlow，脚本会在缺少其密钥时直接记录待配置，不向其他模型发送请求。

## 第 0 阶段完成条件

原应用代码未变，依赖可复现，界面、索引、工具和真实模型问答均有运行记录。当前不以静态检查或工具检查代替真实模型问答成功。
