# Python 内置 Expat 修复

基线为刚真实成功的 `83549c40d04ea83a15945fb1df09f146b042aa1c`、[CI 45575949037](https://github.com/xyma2003/codebase-qa/actions/runs/45575949037)。同一 Python 镜像、解释器版本 3.12.13、模型配置和 Mac/Linux 依赖锁均保留；系统库已更新为 2.11.0，但 Python 实际仍用内置 2.7.4。

仅重建 `pyexpat` 扩展，使用基线镜像 `PYTHON_SHA256` 对应的同版官方 CPython 源码，SHA256 `c08bc65a81971c1dd5783182826503369466c7e67374d1646519adf05207b684`。Expat 头文件使用官方 2.11.0-1 开发包，201132 字节，SHA256 `7f7726333b2b5fd44cf38e78e6e4a7f59cd4e398308102a1805d3aefd2e78e85`，与[官方下载页](https://packages.debian.org/sid/amd64/libexpat1-dev/download)和实际文件一致。

编译参数取基线解释器的 sysconfig，沿用原 Python 头文件和内部 ABI，链接已修复的 `libexpat.so.1`。编译器、头文件和源码只在构建阶段，运行镜像仅复制一个 pyexpat 扩展。构建报告绑定源码、头文件、编译参数和扩展摘要；运行探针检查实际加载文件 SHA256、版本和动态链接一致。没有修改整个解释器或本机共享 Conda 环境。

构建必须通过五组原版 CPython XML 测试：pyexpat、ElementTree Python/C、minidom、SAX。没有跳过失败用例或调整断言；日志和数量归档到 `python-expat-unittest.log` 与 `python-expat-build.json`。应用原生运行、SDK、旧库迁移和恢复仍照常检查。

本机 **450 项全量 / 127.699 秒 / 退出 0**，新增四项源码绑定/流式哈希/重定向/保留旧文件测试，actionlint 与 diff 检查通过。实际 Linux 编译、官方 XML 测试数量和最终双引擎结果由本提交 CI 验收。本次与最近成功基线的系统包应完全一致，镜像对照不允许系统包版本变化。全量系统扫描、公开发布阻断与密钥验收后置保持。
