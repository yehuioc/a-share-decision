---
producer: codex
producer_role: controller
producer_evidence: 用户2026-10-02明确授权公开项目源码并先清理私人内容
review_owner: codex-controller
review_state: reviewed
canonical_status: reference
---

# A 股决策工作台

本机研究工具，将来源证据、独立意见、交叉反证、条件计划和事后复盘连接起来。它不连接券商、不自动交易，也不保证收益。

这是经过隐私清理的公开源码起点，不包含原工作区历史、账户、行情快照、研究结论、对话、付费文章或 API 密钥。测试中的价格与资金均为合成示例。默认观察名单是演示配置，不是推荐。

## 启动

Windows 先运行 `setup.ps1`，再运行 `run.ps1`，浏览器打开 `http://127.0.0.1:8765`。首次依赖安装需要联网。其他平台可创建虚拟环境、安装 `requirements.txt`，运行 `python -m app.cli serve --port 8765`。

模型调用需要自行在本机 `data/model-secret.json` 配置 `api_key`；不要提交该文件。模型参数在 `app/model.py`。调用模型产生费用；后台不会静默换模型。这个源码发布没有运行研究、安装调度或生成投资建议。

## 使用

在页面指定行情与资料渠道，再导入自己有权使用的证据。渠道未指定时保持缺失，不自动选择。默认研究方法和角色在 `roles/`，轮次保存证据、意见、交叉回应和裁决的冻结版本。

实际账户与模拟记录分开；生成条件计划不等于已成交。个人账户、原件、研究记录与密钥都在忽略的 `data/`，应自行备份。财新适配器只消费有权读取的本地归档，公开默认目录为 `data/sources/caixin` 及 `data/sources/caixin-raw`；不提供文章，也不绕过访问限制。

`start-background.ps1`、`install-schedule.ps1` 是可选的本机运行入口；下载源码不会自动安装或启用计划任务。角色与方法不代表预测经过校准。

## 验证

安装 `requirements-dev.txt`，先创建 `.tmp` 目录，再运行 `python -m pytest -q --basetemp .tmp/pytest`。测试验证业务、隔离和错误处理，不证明投资收益。真实联网研究需要自行配置渠道和模型，本次发布未重新执行。

## 许可

当前仅公开源码，未新增开源许可证；公开可读不等于授权任意再分发。第三方依赖遵守各自许可。
