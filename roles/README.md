# 研究方法的来源与适配

本文件位于项目 `roles/README.md`。

本地角色保留独立初稿、一次交叉回应、独立裁决和结果反馈。2026-09-27 按用户要求修正“不明确账户就停止数量研究、只给观察阈值”的行为：已有账户背景进入冻结上下文，账户执行量与假设测算分开，必须解释当前买卖选择。

共同方法维护在 [research-principles.md](research-principles.md)。它吸收用户提供历史对话中的实际问题：混淆持仓与建议、资产日期混用、默认持有、用价格线拖延判断、事后解释和假精确概率。角色加载时自动附上并随新轮次冻结；不照搬旧对话的整套提示词、强制研究门或旧价位。

- [TradingAgents](https://github.com/TauricResearch/TradingAgents/tree/2d17df8da1536c121e4d7395ac5a5dcec9e96d6f)：核读 `tradingagents/agents/managers/research_manager.py` 与 `agents/trader/trader.py`。借鉴权衡最强论据、分歧不等于持有、价格结构支撑交易建议；保留本地冻结证据和反馈链，不引入 TradingAgents 自己的运行与存储栈。
- [AI Hedge Fund Buffett 角色](https://github.com/virattt/ai-hedge-fund/blob/5d2c7ca2d02c6501692a58bb363dfab1916890ba/hedge_fund/signals/buffett.py)：2026-09-27 核读，MIT。借鉴企业所有者、竞争优势、资本配置与估值检查；针对银行和 ETF 改写指标。不采用冒充本人或用自信分充当胜率的表述。只作机制参考，未复制其代码或安装依赖。

第一性原理拆解是本地方法，不声称采用未经核验的“马斯克官方金融提示词”。长期价值与波段时点分开，避免长线理由掩盖短线错误。提示词效果需由后续价格与账户结果检验。

`sizing.scenarios` 是在参考价格下持有相应股数的人民币情景，不代表已成交、历史成本或用户已接受该损失。不得因一个输入缺失而停止整个决策。
