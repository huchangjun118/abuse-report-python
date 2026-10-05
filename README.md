# abuse-report-python

举报受理与账号处置审核工作流（去重归并、案件状态机、处置收敛、申诉、
权限审计、阈值升级、崩溃恢复）。

- 设计与状态转换、配置项说明：见 [`docs/README.md`](docs/README.md)
- 运行：`uvicorn main:app --reload`
- 测试：`pytest -q`
