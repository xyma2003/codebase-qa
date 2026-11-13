# 测试退出挂起修复

日期：2026-11-13。登录与隔离提交 `ab32f60` 后继续核对远端 CI，没有把本地断言成功当作远端完成。

## 原始证据

首次远端 [CI 43386816823](https://github.com/xyma2003/codebase-qa/actions/runs/43386816823) 在北京时间 15:37:50 报告 `Ran 324 tests in 109.652s` 和 `OK`，随后一直不退出，北京时间 16:00:34 被原有 25 分钟超时取消。没有手动重跑或提高超时。原始完整日志保存在本机 `.baseline_runtime/user-access-first-ci-raw.log`。

本机以当前干净提交、空 HF 缓存、离线及空模型密钥做对照，324 项断言全部通过；进程仍不退出。180 秒后的 faulthandler 堆栈显示主线程停在 `threading._shutdown`，一个 AnyIO 工作线程停在 `queue.get`。原始对照日志保存在 `.baseline_runtime/user-access-ci-environment.log`，取证后停止该次已完成断言但未退出的本地进程。

## 修复

移除页面导入时对全局事件循环策略和 `asyncio.run` 的 `nest_asyncio` 补丁。页面模型工作改为独立稳定事件循环，模型客户端可以在同一循环使用；CLI 测试继续使用正常 `asyncio.run` 完成清理。

用户身份随每个任务的 ContextVar 进入模型循环，不使用共享“当前用户”。工具事件通过各请求独立队列回到原页面线程，Streamlit SessionState 不从模型线程读取。页面异常中止时取消自己的 Future，不能影响其他用户任务。模型、嵌入、生成参数、默认检索和原安全上限均未改变。

第一次专项发现模型线程仍读取了页面会话编号，已改为在原页面线程取值后传入；失败日志 `.baseline_runtime/user-access-loop-targeted.log` 保留。修正后 82 项相关测试通过且进程退出成功。

## 最终验证

新增 4 项测试验证：跨请求循环稳定及身份不复用、双用户并发及事件原线程渲染、页面中止取消自己的任务、真实 SDK FunctionModel 与页面导入后子进程可退出。均不访问模型服务商。

空缓存/离线条件下，全量 **328 项通过，123.569 秒，进程返回 0**，见[全量日志](20261113-ui-runtime-unittest.log)与[机器摘要](20261113-ui-runtime.json)。这次验证包含原 24 项登录隔离测试，不增加登录或模型密钥的验收结论。

修复属于页面异步运行生命周期，不是后台任务/配额系统。真实 OIDC、HTTPS Cookie、真实模型与公网验收继续后置。
