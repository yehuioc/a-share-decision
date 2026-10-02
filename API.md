# 工作台业务接口

这是后端、CLI、界面和角色调用共同使用的接口说明。代码只维护一套业务逻辑。金额用十进制计算，JSON 返回数字；未知值是 `null`，不是零。所有时刻采用含时区的 ISO 8601，交易日期为 `YYYY-MM-DD`，展示用北京时间。

## 存储公共接口

`app.store.Store(root=None)` 默认写项目 `data/`。`root` 为测试隔离目录时，不得写默认数据。提供 `read(rel, default=None)`、`write(rel, value, overwrite=False)`、`list(rel)`（该目录直接子项 JSON 对象列表）、`transaction()`（可嵌套、跨进程互斥）、`root`（Path）。`write` 拒绝覆盖非同值对象，所有写入采用原子替换。模块导出 `DomainError`、`ConflictError`、`now_iso()`、`parse_time(str)`、`safe_id(str)`、`stable_id(prefix, request_id)`。标识不含路径字符。

`app.calendar` 提供 `is_trading_day(date|string)`、`next_trading_day(date|string, count=1)`（返回 date）、`expected_market_date(datetime|string)`（返回 date）、`calendar_status(date|string)`（返回含 verified、warning 的对象）。2026 交易所休市表有明确来源；未覆盖年度不冒充已核验交易日历。

## 证据

证据保存于 `evidence/{id}.json`，字段：`id, kind, source_id, title, url, published_at, captured_at, available_at, status, text, metadata`。`published_at` 可空；`available_at` 默认获取时间，不凭封面日期回填历史可知时点。`status` 为 `ready|needs_text|error`；非 ready 不得当已读事实。`metadata.pages` 保留 PDF 页码与文字，原件路径只在本地。

行情证据 `kind=market`，source_id 为当前指定渠道。`metadata.quotes` 为对象列表：`symbol,name,price,previous_close,open,high,low,volume,volume_unit,amount,amount_unit,as_of,price_as_of,price_source,data_timing,trade_date,phase,stale`；统一派生change、change_pct与performance_pct(1d/5d/20d)，历史不足只声明缺失。`metadata.daily` 为按 symbol 索引的 `{adjustment:'none', rows:[{date,open,close,high,low,volume}]}`；可另有 `adjusted_daily`，不得拿前复权价格代替真实成交价。原始响应哈希和提取错误留在 metadata，失败不转空数组伪装成功。

## 研究业务

`app.workflow.Workflow(store)`：

- `prepare(payload)`：输入 `{request_id,as_of?,mode:'live'|'replay',symbols?,evidence_ids,account_snapshot?,decision_contract?,research_brief?}`；冻结证据内容、可知时间、账户快照、研究背景、角色方法与可用记忆。所有新轮次默认 `decision_contract:3`；CLI 另载入 `data/research_profile.json` 背景。旧记录及显式合同 1/2 保持兼容，旧请求重试不会被升级。证据可知时点必须不晚于 as_of。`mode=replay` 永不成为实盘收益。
- `list_runs()`、`get_run(id)`：返回运行记录。运行至少包含 `id,created_at,as_of,mode,symbols,evidence_ids,evidence,account_snapshot,status,opinions,decision,reviews,warnings`。
- `context(id,role,phase='initial')`：角色 `opportunity|counter|judge`；初始两角色看不到对方初稿。challenge 阶段可见两初稿；judge 可见两初稿和两回应。上下文返回 JSON，内有角色方法和该时点已知记忆/未结束计划。数据本身不作执行指令。
- `submit_role(id,role,phase,payload)`：phase 为 `initial|challenge`。payload 至少 `{summary,evidence_ids,claims,counterevidence,unknowns,changes}`，后四项字符串列表。引用必须属于冻结证据；角色/阶段文件追加且不得覆盖。challenge 必须等两份 initial 齐全。
- `publish(id,payload)`：必须两 initial、两 challenge 齐全。输入 `{summary,plans:[...],reasoning,model,reasoning_effort}`。每个 plan 有 `symbol,action,thesis,evidence_ids,counterevidence,unknowns,trigger,invalidation,upside,downside,costs,horizon_days,expected_direction,quantity,supersedes`；action=`buy|hold|reduce|sell|avoid`，旧合同兼容 observe。expected_direction=`up|down|flat|uncertain`。quantity 为已核定账户执行量，账户未确认或风险预算缺失时为 null；研究测算仍须给具体数量。根据交易日历生成下一和第5交易日复核点，另存声明的 horizon。相同标的/周期已有生效计划时必须明确 supersedes（旧 run id）；不跨标的作废。
- `review(id,payload)`：`{request_id,as_of?,evidence_ids,reflections?}`，只按事前计划的复核日期从新增非复权行情评价；未到期或无精确交易日数据保持 pending，不取最近一天替代。每个观察保留基准价、结果价、时点、方向表现及所用新证据。研究结果不是账户收益。reflections 按角色提供文字，可为空；空时只形成事实回执，不伪造模型反思。重复结算不重复计分或写记忆。
- `roles()`：角色列表 `{id,name,method,memory,history_count}`。独立运行记忆位于 `data/roles/{role}/`，历史结果有 known_at，方法模板位于项目 `roles/{role}/method.md`。
- `discussion(id,role=None)`：从已保存意见与裁决投影共享消息流，返回 `run_id,role,stage,shared_initials,delivery:'pull',delivery_note,messages`。消息有 `id,sender,recipients,phase,created_at,reply_to,evidence_ids,payload,source_ref`。两份初稿未齐时公开、对手、裁决角色均不可见，作者只可见自己的初稿；齐备后共享。每条交叉回应指向对手初稿，裁决指向此前四条意见。读取不写新事件，不表示推送、已读或模型接受。
- `context(id,role,phase='decision')`：发布后向指定角色提供同轮共享消息和最终裁决。常规 initial 上下文继续隔离本轮对手；历史只加载本轮冻结时已经存在、且相同 live/replay 模式的本角色意见与既有裁决。

`decision` 至少包含 `published_at,summary,plans,reasoning`。每个 review observation 应含 `symbol,review_date,status,return_pct,direction_hit,source_ids`；预测关联按运行/标的/复核日去重，不解释为来源因果贡献。新增证据或修改初稿需新建 run，不能偷偷修改已冻结一轮。

合同 2 禁止纯 observe，要求每个 plan 另有非空 `decision_basis`（当前选择及替代动作比较）、`long_term_view`（长期持有判断）、`sizing:{basis,assumptions:[string],scenarios:[...]}`。情景字段为 `label,quantity,entry_price,exit_price,stop_price,estimated_fees,reasoning`；quantity 为 100 的正整数倍，有限正价满足 `stop_price < entry_price < exit_price`，estimated_fees 为有限非负数。entry_price 是多头机会成本的比较参考价，不是实际历史买价；即使建议减仓/卖出，仍可以此比较继续持有的上行与下行。页面按 `(情景价格-参考价)×数量-估计费用` 展示人民币结果，不当作实际成交、历史盈亏或概率期望。

`research_brief` 是研究背景，和账本起点分开；已有历史持仓、用户周期与风险预算、来源和信息缺口均应说明。上下文使用冻结副本，后来修改 profile 不改变旧轮次。机械字段校验只能挡住缺项与无效数值；因果推理和价格依据仍由主控审查。

## 人工账本

`app.ledger.Ledger(store)`：

- `set_baseline(payload)`：`{request_id,confirmed:true,as_of,cash,positions:[{symbol,quantity,avg_cost,price}],risk_budget:null|number}`。仅首次建立；初始净值取 price 而非 avg_cost。没有真实用户确认时默认 unconfirmed。
- `record_event(payload)`：`{request_id,as_of,type,status,symbol?,quantity?,price?,amount?,fees?,related_event_id?,dividend_per_share?,note?}`；type=`buy|sell|dividend|deposit|withdrawal|fee`，status=`actual|paper|not_executed`。交易 quantity 为正整数且遵守可交易股数/整手和 T+1；金额不为负。费用 null 表示未知，不能展示精确净利润。补录 fee 可引用未知费用的交易，使其已知；不重复扣除已知费用。paper/not_executed 不改变实际持仓与现金。分红的 `dividend_per_share` 是可选的每股税后金额，用于增减持后的被动对照；已卖空但初始对照仍有股时允许 `amount:0` 与正的每股分红。缺少该信息且股数不同，则对照保持不完整。
- `record_valuation(payload)`：`{request_id,as_of,prices:{symbol:price}}`。价格必须完整正数；记录真实当前估值点供复盘，不从研究计划伪造成交。拒绝重复冲突快照。
- `summary(prices=None,as_of=None)`：至少返回 `confirmed,baseline,positions,cash,net_value,initial_value,net_inflows,net_profit,realized_pnl,unrealized_pnl,dividends,fees,fees_complete,benchmark_value,incremental_profit,monthly_average,complete_months,max_drawdown_amount,events,valuations,warnings,risk_budget`。未知/不完整值为 null，含解释。总净收益按净值口径，不把历史成本形成的初始浮盈重复计入。被动对照保持初始股数、相同净入金及初始持仓对应分红。月均只对边界估值充分的完整月份计算；回撤按剔除外部资金的已记录估值点计算并明确覆盖边界。

## 用户指定渠道

`settings.channels` 默认 `{market:null,trading:null,caixin_enabled:false}`。`PUT /api/settings` 或 `settings --input <JSON>` 可更新其中任意字段。market 为 `{name,reference?,mode:'manual'|'tencent'}`，trading 为 `{name,reference?,mode:'manual'}`，null 表示撤销；系统产生稳定 `source_id`。name 是用户明确指定的平台/文件来源，reference 是网址或说明；不会向任意 reference 发请求。交易信息渠道仅记录来源，不连接券商。只有明确选用 tencent 模式才允许现有在线适配器取价，不支持的渠道采用人工输入，绝不自动回退。

`collect`、`POST /api/market/refresh`、`sync-caixin` 与对应 HTTP 同步均在实际读取前检查渠道。人工行情由 `import-market --input <JSON>` 或 `POST /api/market/import` 输入：`{request_id,quotes:[{symbol,name?,price,as_of,phase:'closed'|'intraday'}],daily?:{<symbol>:{adjustment:'none',volume_unit?,rows:[{date,open,close,high,low,volume}]}}}`。as_of 含时区，保留原始报价时间，获取时间由系统填写。日线拒绝未来/休市日期、未完成的当日日线、重复日期、OHLC矛盾和收盘价冲突；单位未知不能擅自当作股数。每次最多12个标的。既有来源版本保留。

新 live prepare/publish/review 校验行情来自当前指定渠道；新轮冻结 `channels` 与 `research_brief`，后者未显式传入时从本地 profile 加载。研究期间改换渠道须重新准备，不能拿旧渠道证据发布。历史轮次可继续读取和幂等重试；replay 不冒充新 live 研究。列表返回 `channel_review_required`，页面将需复核的旧裁决留在历史中。未获当前渠道认可的保存价格不进入当前账户估值。

角色方法由各自 `roles/<role>/method.md` 和共同 `roles/research-principles.md` 合成，再冻结进轮次；共同方法修改不覆盖旧快照。

## HTTP 与 CLI

HTTP 只监听 `127.0.0.1`。业务写请求要求本地同源与会话 CSRF token。`GET /api/state` 返回 `{settings,market,evidence,sources,runs,roles,ledger,paper_ledger,packet_status,performance,system}`。`system` 提供 `csrf_token,version,data_root`；页面所有展示用 textContent，原文不执行 HTML。

`state.runs` 是供列表与计划展示的摘要，不重复传输原始大文件；完整冻结正文、账户和各阶段意见由单轮接口读取。`state.sources` 是来源对象数组。角色意见的正文位于 `opinions.<role>.<phase>.payload`；经验的 `facts` 是复核对象数组，`reflection.text` 为可选人工或模型反思。复盘状态位于 `reviews[].observations[].status`，没有结果时仍按 `decision.plans[].review_points` 显示待核对。

风险限额在界面 settings 修改后带 `risk_budget_updated_at`，账本当前摘要优先使用明确更新的 settings 值并保存来源；未设置时使用首次基线值。工作流只冻结该时点的账户摘要，不另读动态设置。历史时点无法还原的限额保持未知。

完整月均只在自然月结束后计算：需要月初开盘前建立的基线或上一月最后交易日的收盘估值，以及本月最后交易日的收盘估值；月底盘中记录不算完整边界。月份现金事件计算至自然月末，净入金被剔除。当前摘要时点取当前时间，未来模拟记录不会把实盘时点推进到未来。最大回撤包含确认基线与随后已记录估值点，点间波动未知。

实际交易原费用未知且经用户确认实际为零时，可补录 `type:'fee', amount:0, related_event_id:<原交易>`；没有关联交易时不接受零金额费用。后月未知费用不会抹掉此前已完整月份的结果；补录历史交易使旧估值缺价时，该月或回撤保持未知并注明原因。

| 请求 | 用途 |
|---|---|
| GET /api/state | 全部界面状态，market.latest 为最近行情证据或 null |
| PUT /api/settings | `{watchlist?,risk_budget?,channels?}`，不接 API 密钥 |
| POST /api/market/refresh | 仅刷新用户选定且已支持的在线渠道，不调用模型 |
| POST /api/market/import | 保存用户按指定渠道提供的报价/未复权日线 |
| POST /api/materials/text | `{request_id,title,source_id,url?,published_at?,text}` |
| GET /api/materials/search | `q,source_id,category,issue,status,offset,limit`；返回 `items,total,offset,limit,categories,issues`，默认每页 25 条 |
| GET /api/sources/caixin | 本地财新连接、来源快照和累计版本库状态 |
| POST /api/sources/caixin/sync | 用户启用后接收 `{request_id}`；只读本地 ingestion，新增或复用不可变资料版本 |
| POST /api/materials/file | multipart `file,title,source_id,url?,published_at?`；文本或 PDF，20MiB 限额 |
| POST /api/source-checks | `{request_id,source_id,claim,verdict:'correct'|'incorrect'|'unverified',evidence_ids,note}` |
| GET /api/evidence/{id} | 单份证据全文、页码、元数据 |
| GET /api/runs/{id} | 独立意见、交叉回应、裁决与复盘 |
| GET /api/runs/{id}/discussion | 可选 `role`；按阶段与角色投影共享消息 |
| GET /api/roles/{role} | 方法与角色记忆 |
| POST /api/ledger/baseline | 确认最简初始账本 |
| POST /api/ledger/events | 记录实际/模拟/未执行事件 |
| POST /api/ledger/valuations | 保存估值点 |

写成功直接返回业务 JSON，不额外包 data。业务错误返回 `{detail:'中文原因'}` 和 400/409/422；页面必须显示错误，不能成功提示。CLI 使用同一 Store/Workflow/Ledger；支持 init、collect、import-text、import-file、prepare、context、opinion、publish、review、ledger、serve。CLI 可用 JSON 文件提交角色稿件；独立后台直接调用相同业务接口，页面研究按钮启动真实后台任务。

## 本地财新来源

`app.caixin.sync_caixin(store,request_id=...)` 只读本地清洗 JSON与原件。目录优先采用显式 `root`、`raw_root` 参数，其次读取本地 `data/source_status/caixin.json` 的同名字段，未配置时使用项目 `data/sources/caixin` 与 `data/sources/caixin-raw`。只采用 `faithful_body`，不把 enrichment 或摘要当成原文。正文与原件哈希、完整性和缺页检查通过才标记 ready；受限、缺页、缺失或哈希异常保留 needs_text 与具体原因。

证据的 `available_at` 为首次实际接入时刻；旧发布日期不会使资料穿越进入过去的研究。内容或完整性状态改变形成新版本，已冻结研究仍引用旧版本。`metadata` 保留文章标识、栏目、期次、发布日期精度、来源存档时间、清洗文件相对路径、原件路径与哈希、段落字符范围。网页段号用于定位清洗正文，不伪称 PDF 页码。

同步返回 `scanned,created,existing,ready,blocked,invalid,gaps,status`。`status` 顶层有 `total,ready,blocked,latest_published_at,last_sync`；`source_snapshot` 是最近扫描快照，`library` 是累计文章与版本数量，旧版本不会删除。稳定 request_id 用于同次重试；再次扫描使用新 request_id。CLI 对应 `sync-caixin --request-id <标识>`、`caixin-status`、`search --query <关键词> --source caixin --limit 25 --offset 0` 和 `discussion <run_id> [--role <角色>]`。

这条连接同步已有本地资料，不自行联网抓取新一期；启用后由独立后台在每轮研究前调用。事实核验、来源获取延迟、工作台导入延迟和事前判断的预测关联分开显示；没有结果样本时保持待评，不因来源名称或导入数量生成可信度分数。

## 独立模型与运行任务

`app.model.DeepSeek.complete(context,contract)` 直接请求官方流式 Chat Completions，返回 JSON 结果与调用审计。固定参数由 `app.model.MODEL` 维护；密钥读取 `data/model-secret.json`，公共状态仅含 configured，不返回密钥。只有正常 stop、可解析对象、实际 model 和 usage 齐全才完成。网络停滞超时不等于总思考时限。

`app.runner` 保存任务、阶段缓存与跨进程锁。状态有 queued/running/completed/blocked/failed/interrupted；blocked 表示渠道或行情需补充，failed/interrupted 可继续同轮。已发布但结果回传失败时保留裁决，恢复只补未完成阶段。请求 id 保持幂等；重复注册定时任务不会建立另一套研究实现。

`python -m app.runner catch-up` 用于资料导入后的补跑。只检查最近定时时段的blocked任务；未就绪返回waiting_for_inputs和具体gaps，不调用模型。就绪后按时段和输入版本幂等创建late-delivery任务，并保留catch_up_for；该时段已补跑完成则返回not_needed。输入版本已尝试则返回already_attempted，失败研究仍从既有任务恢复。原blocked记录和实际执行时间不改写。

- `GET /api/state` 增加 `automation:{model,schedule,jobs}`。任务公开状态不含完整上下文或密钥。
- `POST /api/automation/start`：`{request_id}`，建立后台任务并返回任务对象；遵守现有 CSRF 边界。
- `POST /api/automation/jobs/{id}/resume`：`{}`，继续失败或中断阶段；资料阻塞补齐后新建轮次。
- `python -m app.runner scheduled|start|status`；`job <id>` 执行已有任务，`replay <run_id>` 在指定隔离数据目录回放。所有命令支持 `--data-dir`。

后台先按来源筛选有效资料，不使用隐藏网络搜索。两初稿独立读取，随后一次交叉回应、裁决和两份读取回应；相同模型的角色不能当作统计独立。完成阶段先持久化，再提交对应工作流事件，恢复可复用阶段结果；完整调用审计保存到 `model_calls/`。

## 独立角色 Agent

三个角色分别由 `roles/opportunity/agent.json`、`roles/counter/agent.json`、`roles/judge/agent.json` 配置。`app.agents.RoleAgent` 为每个冻结配置创建独立客户端实例，所有实例读取同一个 `data/model-secret.json` 和 `app.model.MODEL`；配置文件不复制密钥。

`Workflow.prepare` 将各自 agent 配置及其内容哈希一同冻结。各阶段按 role 路由给对应 agent；同轮的初稿、回应、读取裁决和后续复盘使用该 agent 的会话，拒绝跨角色上下文。会话记录保存于 `data/agents/<agent_id>/sessions/<run_id>/`。会话记录不直接拼回输入，未来信息过滤和历史记忆仍由 Workflow 负责。

`agent_id` 是稳定角色身份，`call_id` 是单次模型调用，`agent_session_id` 是角色与轮次共同确定的会话。模型审计记录代码指定身份与实际返回型号；原有历史记录保持原样。`GET /api/state` 的 `roles[].agent` 和 `automation.agents` 展示配置、公共模型参数及独立会话数，不返回密钥。资料筛选继续使用调度辅助客户端，不混入三角色会话。


## 研究情报交付

`GET /api/packets/template` 返回当前来源绑定、简化的 `template` 与完整 `prompt`；提示词来自 `prompts/intelligence-collection.md`，不复制第二份。`POST /api/packets/import` / `import-packet --input <JSON>` 导入 `research-packet-v2`，旧 `decision-packet-v1` 仍可读取。`POST /api/packets/sync` / `sync-packets` 接收 `data/packet_inbox/*.json`；只做本地接收。状态由 `/api/packets/status` / `packet-status` 查询。

顶层只需 `schema,request_id,message_id,source_reference,as_of,session,text`，可附 `market`。session为midday/close/non_trading；as_of是来源实际交付时点，不能在未来。source_reference必须属于当前trading.reference或additional_references；除HTTP(S)来源外，允许已绑定的 `codex-automation:<任务ID>` 作为本地主采集者身份。这个内部身份不能作为price_source或事实原始链接，底层证据仍须HTTP(S)出处。消息身份、实际导入时间和来源绑定不可伪造或覆盖。原始JSON存source_snapshots，正文完整保存，available_at为真实接入时间。

主绑定为本地采集者时，packet-status优先返回已收到的本地主包；只有主包尚未到达才带限制显示网页补充。较晚网页输出不会取代本地主包，仍可作为研究端补充资料。返回source_role为primary或supplement。行情独立选择腾讯时，包内market不覆盖主行情；本地主包可省略market，引用本次collect保存的真实行情证据。

正文说明主价格、相关事实、市场变化、近期事件及缺口影响，保留可打开的底层链接。coverage、facts/events逐条元数据、valid_until、comparison、holding_state、baseline_state不再是交付前提。缺链接、旧日期或原包未解决冲突变成具体使用限制，不阻断整包。READY仅表示正文可接收，不能代替事实核实或各标的价格检查。

market.quotes每项最少为 `symbol,name,price,as_of,phase,price_source`。price须为真实正数；as_of是实际报价时点；phase为closed/intraday；price_source为底层来源URL。同步程序从源端最后的JSON提取quotes，补齐消息身份和接收字段。即使quotes为空也保留正文。其他渠道不能借包内价格绕过用户的行情来源选择。

程序按标的采用有效价格：辅助行情错误时保留主价格并说明停用范围；主价格无效、重复、日期错误或与同日收盘冲突时跳过该标的。当前有效标的进入request.symbols，原观察名单、未采用标的与理由进入research_brief.data_scope，随轮次冻结并显示。所有标的都无有效价格时才在模型调用前停止定量研究。午间研究允许在13:30前使用当日上午11:25之后的快照，明确它是上午研究基准，不能称为当前执行价或正式收盘价。

market.daily可选，按代码保存adjustment:none、volume_unit、rows；每行date、open、high、low、close、volume（可null），amount可选。取得且一致的未复权历史用于1/5/20交易日变动和精确日期复核。缺历史不阻断新研究，依赖日线的旧计划仍待补，不伪造路径。单独 `/api/market/import` 保留其人工行情一致性检查。

状态同时返回as_of、session、source_reference、imported_at和具体warnings，区分源端时间、导入时间与价格时点。upstream_ready_state/upstream_gaps仅保留原文声明，不能重新成为统一阻断条件。账户连接不属于情报采集功能；账本和历史账户背景由本地已有模块处理。

## 条件路径、执行归因与学习

合同3意见必须包含forecasts:[{symbol,horizon_days,expected_direction,rationale,falsifier}]，覆盖每个标的1/5/20日；方向允许uncertain，其结果为abstained而非命中。裁决顶层包含argument_resolution:{opportunity,counter}。

每个plan增加edge与execution。edge含mechanism、alternative_explanation、priced_in、falsifier、probability_basis(unknown/subjective/empirical)、win_probability、sample_refs、independent_samples；无依据概率为空，经验概率要求冻结样本引用。scenario_metrics计算零摩擦和费用敏感性的盈亏平衡概率；这些是二分支算术，不是已验证的期望收益。

execution含strategy_key、assumptions、legs。每个leg含action、quantity、entry:{kind,price}、take_profit、stop_loss、exit_basis、conditions、buy_fee、sell_fee、slippage_bps、reasoning。entry支持next_open/limit/stop/close_above/close_below/manual/none；hold/avoid为none，reduce分成互不重叠的hold与sell分仓。费用未知null。quantity为研究假设，不覆盖账户执行量。

发布时程序固定plan_id、valid_from、valid_until；盘中或盘后发布从次一完整交易日开始日线回放，收盘条件从次日开盘模拟。买入当日T+1不可卖出，同日无法确定止盈止损先后时未决。到期只按收盘估值，不造卖单。非价格条件无法通过日线推定。旧合同没有结构规则时返回legacy_unstructured；失效/替代后原规则回放返回terminated_counterfactual，不能记作有效计划成绩。

`GET /api/runs/{id}/outcomes` / `plan-outcomes <id>` 返回原规则模拟与人工执行归因。`POST /api/plans/events` / `plan-event --input` 接收request_id,run_id,plan_id,kind(triggered/not_executed/invalidated/expired),as_of,source,note。不可覆盖旧事件，不允许早于发布或晚于现在。

账本buy/sell/dividend可一起提供run_id和plan_id。标的和发布时间必须匹配，replay不能关联actual。关联交易的incremental_trade_pnl为净现金流+数量变化×复核价−实际费用，比较的是“不执行这些交易”；不等于账户总利润，也不包含分红差额。闭合回合仅在关联净数量为零且费用已齐时给出净额。入金不计计划盈利。记录有recorded_at，后补交易不进入当时尚未知的计划复盘。

`POST /api/runs/{id}/review`保存不可变plan_outcomes、role_assessments与各角色反思；下一轮只加载当时已知结果。每个角色按自己初稿/回应评分，不能借最终裁决方向当自己的成绩。

## 模拟与项目运行成本

`Ledger(store, mode='paper')`使用独立paper-baseline和paper-valuations，事件仍带status:paper；借用真实现金或卖出当天买入数量均不允许。HTTP为`/api/ledger/paper/baseline`、`/api/ledger/paper/valuations`；事件通过原events接口按status路由。CLI ledger增加`--mode paper`。

`POST /api/project-costs` / `project-costs --input`接受request_id,start_date,end_date,amount,currency:CNY,confirmed_complete:true,source。日期必须已经结束且区间不重叠；金额须包括对应期间所有模型、数据和分摊订阅账单。不能猜汇率或将token当账单。`GET /api/performance?as_of=<ISO>` / `performance --as-of <ISO>`可按历史时点核对；当日账单未结束时不显示扣成本的确定利润。

扣成本月均只计算账本边界充分且成本覆盖完整自然月的月份；跨月账单必须有依据分拆后登记，程序不任意摊分。策略按strategy_key与重叠期限分时间簇，避免把连续几天相同判断累计为独立胜率；不自动调整收益目标或风险预算。
