# Python 内置 Expat 修复

基线为刚真实成功的 `83549c40d04ea83a15945fb1df09f146b042aa1c`、[CI 45575949037](https://github.com/xyma2003/codebase-qa/actions/runs/45575949037)。同一 Python 镜像、解释器版本 3.12.13、模型配置和 Mac/Linux 依赖锁均保留；系统库已更新为 2.11.0，但 Python 实际仍用内置 2.7.4。

首项只重建 `pyexpat`，随后按下文兼容性复核扩展为同时重建 `_elementtree`。两者使用基线镜像 `PYTHON_SHA256` 对应的同版官方 CPython 源码，SHA256 `c08bc65a81971c1dd5783182826503369466c7e67374d1646519adf05207b684`。Expat 头文件使用官方 2.11.0-1 开发包，201132 字节，SHA256 `7f7726333b2b5fd44cf38e78e6e4a7f59cd4e398308102a1805d3aefd2e78e85`，与[官方下载页](https://packages.debian.org/sid/amd64/libexpat1-dev/download)和实际文件一致。

编译参数取基线解释器的 sysconfig，沿用原 Python 头文件和内部 ABI，链接已修复的 `libexpat.so.1`。编译器、头文件和源码只在构建阶段，运行镜像最终仅复制两个 XML 扩展。构建报告绑定源码、头文件、编译参数和扩展摘要；运行探针检查实际加载文件 SHA256、版本和动态链接一致。没有修改整个解释器或本机共享 Conda 环境。

构建必须通过五组原版 CPython XML 测试：pyexpat、ElementTree Python/C、minidom、SAX。没有跳过失败用例或调整断言；日志和数量归档到 `python-expat-unittest.log` 与 `python-expat-build.json`。应用原生运行、SDK、旧库迁移和恢复仍照常检查。

本机 **450 项全量 / 127.699 秒 / 退出 0**，新增四项源码绑定/流式哈希/重定向/保留旧文件测试，actionlint 与 diff 检查通过。实际 Linux 编译、官方 XML 测试数量和最终双引擎结果由本提交 CI 验收。本次与最近成功基线的系统包应完全一致，镜像对照不允许系统包版本变化。全量系统扫描、公开发布阻断与密钥验收后置保持。

## 首次验收与复核发现

`a22852c` 的 [CI 45577310346](https://github.com/xyma2003/codebase-qa/actions/runs/45577310346)全部为绿。Mac **450 项/115.681 秒**；Linux 五组官方 XML 回归总结为 **792 项/1.281 秒、29 条跳过记录**。日志摘要与内容 SHA 已核对，但随后逐条复核发现其中 19 项为 `requires _elementtree`，不能算正常环境跳过或完整 C 兼容验收。

系统 Expat 和 Python Expat 实际均为 **2.11.0**；Python 仍为 3.12.13，Python 扩展 SHA256 `a451b5bdfa8dcd573f428bc5303c662238c7f2170c50a6e5c03f1b5bca9dcaaf` 与构建报告一致，ldd 确认链接系统 libexpat.so.1。候选镜像 `sha256:641671d10de798fc059576f037bb20acfe561a02b025cb049378718b5d1fd085`，Linux 产物 SHA256 `d278c0337d90fa8295e309a7fd235e225d30131342ed5dfe737983a28efb9c32` 已下载验证。

118 个系统包与最近成功基线完全一致；195 个 Python 包审计完整、0 组命中。旧底座数据迁移、新镜像恢复和双用户隔离通过。原业务数据只读回查：4 集合/1256 向量、2 会话/3 轮，内容摘要仍为 `9fd14008178680c699d441cd6e83dd0056b35b1f4c0faf206df92afec7de647f`。

完整系统扫描仍为 **342 条/179 编号/22 个不同 High**，未忽略任何命中、未生成 VEX、未改变发布门槛。此次验证的是目标 Expat 修复，不代表 Expat 所有历史公告或其他系统组件都已清除。下一项优先核对新增 OpenSSL 107494 及其余系统公告；模型、OIDC、目标主机真实验收仍后置。

## ElementTree C 兼容修复

19 项跳过均在 C 模块相关用例，包括 `TestAcceleratorImported`。核对[原版 _elementtree.c](https://github.com/python/cpython/blob/v3.12.13/Modules/_elementtree.c)确认：模块初始化要求 C API 的 Expat major/minor/micro 与编译时完全相同，否则抛出 `pyexpat version is incompatible`；ElementTree 的 Python 包会回退到纯 Python。只重建 pyexpat 不够。

本次使用同一份官方 CPython 源码、同一 2.11 头文件和原 ABI 一并重建 `pyexpat` 与 `_elementtree`，编译器仅在构建阶段。构建和运行都显式导入 C 模块，要求标准 ElementTree 的 Element/XMLParser 与 C 类型同一、XMLParser 报 2.11，并核对两份扩展摘要。官方日志如出现 `requires _elementtree` 跳过直接失败并保留具体用例，不调整断言或放行该跳过。有效配置基线仍为 `83549c4`，真实 C 兼容与新增测试结果由本提交 CI 再验收。

本机修正后 **451 项/126.719 秒/退出 0**，新增反向测试确保成功的 unittest 总结不能掩盖 C 扩展缺失。参数、源码、头文件、解释器和依赖锁不变；只补充必需的 C 扩展同步编译与验收。

## 最终 C 实现验收

`e01236f` 的 [CI 45579783595](https://github.com/xyma2003/codebase-qa/actions/runs/45579783595) Mac/Linux 全部通过：项目 **451 项/131.148 秒**；五组官方 XML 回归总结为 **791 项/0.651 秒、12 条上游条件跳过记录**。跳过记录包含子用例，不能直接用总数相减计算通过数量；其中含大内存测试的 2 GiB 额度条件。已逐条复核：`requires _elementtree` 跳过为 **0**，C 导入与 C 解析器身份用例通过。构建和实际运行都确认 ElementTree 使用 C Element/XMLParser 且版本为 Expat 2.11.0，C 模块已恢复。

两份 Expat 都实际加载 2.11，原 Python 保持 3.12.13。实际 pyexpat SHA256 `a451b5bdfa8dcd573f428bc5303c662238c7f2170c50a6e5c03f1b5bca9dcaaf`，ElementTree C 扩展 SHA256 `b83a720ac2b89d33a7c33e241b8848ab2ff937893641d7cced2d370b6b5d7915`，均与构建一致。Linux 产物 SHA256 `92c561296ada2f54eb22cd6cb894542a08d42ec51832c2b7f211836069679b21` 已下载核对，镜像为 `sha256:98de3ed5d016b20af85a4bd728fb28201f74a91279111b5c5700cca8d4fcd195`。

与最近有效基线的 118 个系统包版本完全一致；195 个 Python 包审计完整、0 组命中，旧数据迁移、新镜像恢复、双用户隔离通过。原 4 集合/1256 向量、2 会话/3 轮保留，会话内容摘要不变。完整系统扫描仍为 342 条/179 编号/22 个不同 High，严格发布阻断保留。下一项处理 OpenSSL 新公告及其他系统风险，密钥相关验收后置。
