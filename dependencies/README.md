# 当前运行依赖

- `runtime-pins.txt`：当前公共包的精确版本，作为 Mac 和 Linux 的共同输入。
- `runtime-macos.lock`：Python 3.12 / macOS 安装锁，包含 PyPI SHA256。
- Linux CPU 锁：CI 使用 `requirements.txt` 和 `runtime-pins.txt` 解析，逐项检查公共版本，再归档为 `runtime.lock`。Linux 额外依赖及 PyTorch 的 `+cpu` 标记单独记录。

全新 Mac 虚拟环境按哈希安装：

```bash
uv pip install --require-hashes -r dependencies/runtime-macos.lock
uv pip check
```

更新既有环境时使用 `uv pip sync --require-hashes dependencies/runtime-macos.lock`，以移除旧元包。先备份原环境，升级和回滚均在停止服务后进行；不能只更换源码而沿用不匹配的依赖环境。

Pydantic AI 使用官方精简发行包 `pydantic-ai-slim[anthropic,openai]==1.107.7`，Python 导入名仍为 `pydantic_ai`。移除全量适配器元包，避免启用未使用的 Hugging Face 适配器约束。其余既有包暂时保留；这次没有整体清理或升级依赖树。

原始环境锁仍在 `docs/baseline/runs/20261110-stage0/`，用于原版复现。当前锁不能覆盖它。每次整改记录精确版本差异、官方文件哈希、完整测试和新的漏洞查询；存在未处理公告时发布继续阻断。
