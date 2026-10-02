(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = { data: null, csrf: null, busy: false, tab: 'decision', role: null, caixinStatus: null, caixinLastSync: null, automationTimer: null };
  const tabs = {
    decision: ['DECISION DESK / 01', '当前决策'],
    roles: ['METHODS / 02', '角色与记忆'],
    materials: ['EVIDENCE / 03', '资料与来源'],
    ledger: ['LEDGER / 04', '记录与复盘']
  };

  const arr = value => Array.isArray(value) ? value : [];
  const obj = value => value && typeof value === 'object' ? value : {};
  const display = value => value === null || value === undefined || value === '' ? '—' : String(value);
  const fmtDate = value => value ? String(value).slice(0, 10) : '未确定';
  const fmtTime = value => {
    if (!value) return '未记录';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : `${date.toLocaleString('zh-CN', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Shanghai' })}（北京时间）`;
  };
  const num = (value, digits = 2) => {
    if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) return '待确认';
    return Number(value).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  };
  const money = value => value === null || value === undefined ? '待确认' : `¥${num(value)}`;
  const text = (tag, value, className = '') => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = display(value);
    return element;
  };
  const button = (label, className = 'btn', handler, attrs = {}) => {
    const element = text('button', label, className);
    element.type = attrs.type || 'button';
    Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, value));
    if (handler) element.addEventListener('click', handler);
    return element;
  };
  const card = (children, className = 'card') => {
    const element = document.createElement('div');
    element.className = className;
    (Array.isArray(children) ? children : [children]).filter(Boolean).forEach(child => element.append(child));
    return element;
  };
  const sectionHead = (title, subtitle, tools = []) => {
    const wrap = document.createElement('div'); wrap.className = 'section-head';
    const left = document.createElement('div'); left.append(text('h2', title), text('p', subtitle)); wrap.append(left);
    if (tools.length) { const right = document.createElement('div'); right.className = 'section-tools'; right.append(...tools); wrap.append(right); }
    return wrap;
  };
  const empty = (title, detail) => card([text('strong', title), text('p', detail)], 'empty');
  const action = fn => withBusy(async () => { try { await fn(); } catch (error) { notice(error.message, 'error'); } });

  function notice(message, type = '') {
    const element = $('#notice'); element.textContent = message; element.className = `notice ${type}`; element.hidden = !message;
  }

  function requestId(form) { if (!form.dataset.requestId) form.dataset.requestId = crypto.randomUUID(); return form.dataset.requestId; }

  function beijingIso(value) {
    if (!value) return null;
    const local = String(value).trim();
    if (/[zZ]|[+-]\d\d:?\d\d$/.test(local)) return new Date(local).toISOString();
    return new Date(`${local.length === 16 ? `${local}:00` : local}+08:00`).toISOString();
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    if (options.method && options.method !== 'GET') {
      if (!state.csrf) throw new Error('本地服务未提供 CSRF token，写入已停止');
      headers.set('X-CSRF-Token', state.csrf);
    }
    const response = await fetch(path, { ...options, headers });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* no JSON body */ }
    if (!response.ok) throw new Error(payload.detail || `请求失败（${response.status}）`);
    return payload;
  }

  async function loadState() {
    $('#loading').hidden = false; notice('');
    try {
      state.data = await api('/api/state'); state.csrf = obj(state.data.system).csrf_token || null;
      $('#loading').hidden = true; $('#system-status').textContent = '本地服务已连接'; $('.status-dot').classList.add('ok');
      $('#asof-label').textContent = `状态更新 ${fmtTime(new Date().toISOString())}`; render();
    } catch (error) {
      $('#loading').hidden = true; $('#system-status').textContent = '本地服务不可用'; $('.status-dot').classList.remove('ok'); notice(error.message, 'error'); renderOffline();
    }
  }

  function renderOffline() { Object.keys(tabs).forEach(id => $(`#view-${id}`).replaceChildren(empty('尚未连接到本地服务', '请确认 FastAPI 服务已在 127.0.0.1 启动，再点击右上角重新加载。'))); }

  function setTab(id) {
    state.tab = id;
    if (id !== 'decision' && state.automationTimer) { clearTimeout(state.automationTimer); state.automationTimer = null; }
    $$('.nav-tab').forEach(tab => { const active = tab.dataset.tab === id; tab.classList.toggle('active', active); tab.setAttribute('aria-selected', String(active)); });
    Object.keys(tabs).forEach(key => { const view = $(`#view-${key}`); view.hidden = key !== id; view.classList.toggle('active', key === id); });
    $('#page-eyebrow').textContent = tabs[id][0]; $('#page-title').textContent = tabs[id][1]; if (state.data) render();
  }

  function withBusy(work) {
    if (state.busy) return Promise.resolve();
    state.busy = true;
    const buttons = $$('button'); const disabled = new Map(buttons.map(element => [element, element.disabled]));
    buttons.forEach(element => { element.disabled = true; });
    return Promise.resolve(work()).finally(() => { state.busy = false; buttons.forEach(element => { element.disabled = disabled.get(element); }); });
  }

  function render() { if (state.tab === 'decision') renderDecision(); if (state.tab === 'roles') renderRoles(); if (state.tab === 'materials') renderMaterials(); if (state.tab === 'ledger') renderLedger(); }

  function metric(label, value, note) {
    const element = document.createElement('div'); element.className = 'card metric-card';
    element.append(text('div', label, 'metric-label'), text('div', value, `metric-value ${['待确认', '待积累'].includes(value) ? 'pending' : ''}`), text('div', note, 'metric-note')); return element;
  }

  function quoteRows(latest, watchlist) {
    const quotes = arr(obj(latest.metadata).quotes), warnings = arr(obj(latest.metadata).warnings), bySymbol = new Map(quotes.map(quote => [quote.symbol, quote]));
    const symbols = watchlist.length ? watchlist : quotes.map(quote => ({ symbol: quote.symbol, name: quote.name }));
    if (!symbols.length) return empty('尚无观察标的', '请先编辑观察名单。');
    const table = document.createElement('div'); table.className = 'quote-strip';
    symbols.forEach(item => {
      const quote = bySymbol.get(item.symbol); const chip = document.createElement('div'); chip.className = 'quote-chip';
      chip.append(text('b', `${item.symbol} ${item.name || quote?.name || ''}`));
      chip.append(text('span', quote ? `${num(quote.price, quote.symbol.startsWith('5') ? 3 : 2)} · ${({closed:'已收盘',intraday:'盘中'})[quote.phase] || '状态待确认'} · ${fmtTime(quote.as_of)}` : '尚无真实行情'));
      if (quote) {
        const detail = document.createElement('details'); detail.append(text('summary', '开高低、量额与来源'));
        detail.append(text('p', `前收 ${num(quote.previous_close,3)}；开 ${num(quote.open,3)}；高 ${num(quote.high,3)}；低 ${num(quote.low,3)}；涨跌 ${num(quote.change,3)}（${num(quote.change_pct)}%）`),
          text('p', `成交量 ${num(quote.volume,0)} ${quote.volume_unit || '单位未知'}；成交额 ${money(quote.amount)}`),
          text('p', `1/5/20日价格变化：${[1,5,20].map(h => quote.performance_pct?.[`${h}d`] == null ? '待补' : num(quote.performance_pct[`${h}d`])+'%').join(' / ')}`),
          text('p', `来源：${quote.price_source || '待补'}；时间口径：${quote.data_timing || '待补'}`, 'subtle'));
        chip.append(detail);
      }
      if (quote?.stale) chip.append(text('span', ' · 陈旧', 'source-eval')); table.append(chip);
    });
    if (warnings.length) table.append(text('p', `行情警告：${warnings.join('；')}`, 'subtle')); return table;
  }

  function activePlans(runs) {
    const all = runs.filter(run => run.mode !== 'replay' && !run.channel_review_required).flatMap(run => arr(obj(run.decision).plans).map(plan => ({ ...plan, run })));
    return all.filter(item => item.run.status === 'published' || item.run.status === 'reviewed').filter(item => item.active !== false).sort((a, b) => new Date(b.run.created_at || b.run.as_of) - new Date(a.run.created_at || a.run.as_of));
  }

  function reportedAccountPanel() {
    const profile = obj(state.data?.research_profile), trades = arr(profile.reported_trades), positions = arr(profile.current_positions);
    if (!trades.length && !positions.length) return null;
    const panel = card([text('h3', '已同步的交易与持仓背景')]);
    trades.forEach(trade => panel.append(text('p', `${trade.symbol} ${trade.side === 'sell' ? '已卖出' : '已买入'} ${num(trade.quantity, 0)} 股 · 成交价 ${num(trade.price, 2)} 元 · 成交额 ${money(trade.gross_amount)}；成交日期：${trade.date || '未提供'}，报告于 ${fmtTime(trade.reported_at)}。`)));
    positions.forEach(position => panel.append(text('p', `${position.symbol} 剩余持仓参考：${num(position.quantity, 0)} 股。${position.basis || ''}`)));
    panel.append(text('p', '新研究使用此处更新后的持仓背景。旧计划保留发布时的数量与条件；成交报告不自动生成账户净收益。', 'subtle'));
    if (!state.data?.ledger?.confirmed) panel.append(text('p', '当前现金和账户起点尚未确认。', 'subtle'));
    return panel;
  }

  function renderDecision() {
    const view = $('#view-decision'); view.replaceChildren(); const data = state.data || {}, settings = obj(data.settings), market = obj(data.market), latest = obj(market.latest), runs = arr(data.runs), plans = activePlans(runs), watchlist = arr(settings.watchlist), warnings = arr(latest.metadata?.warnings);
    const channels = obj(settings.channels), marketChannel = channels.market;
    const latestDecisionRun = runs.filter(run => run.mode !== 'replay' && !run.channel_review_required && ['published', 'reviewed'].includes(run.status) && run.decision?.summary).sort((a, b) => new Date(b.decision?.published_at || b.created_at || b.as_of) - new Date(a.decision?.published_at || a.created_at || a.as_of))[0];
    const decisionSummary = latestDecisionRun?.decision?.summary || '';
    const headline = decisionSummary ? decisionSummary.split(/(?<=[。！？])/)[0] : marketChannel ? '按你指定的渠道重新核对后，形成新的买卖判断。' : '股价与交易信息渠道，由你指定。';
    const heroDescription = decisionSummary.slice(headline.length).trim() || (latestDecisionRun ? `最新裁决来自 ${fmtDate(latestDecisionRun.as_of)}。` : marketChannel ? '已指定情报来源，等待本轮必需行情与证据齐备。下方分别显示资料接收和研究执行状态。' : '尚未指定行情来源。历史资料与裁决保留在下方研究轮次。');
    const hero = document.createElement('div'); hero.className = 'hero'; const copy = document.createElement('div'); copy.append(text('div', '今日工作面', 'eyebrow'), text('h2', headline), text('p', heroDescription)); hero.append(copy, text('span', latest.metadata?.quotes?.length ? '行情证据已记录' : '行情待获取', 'hero-badge')); view.append(hero);
    const accountPanel = reportedAccountPanel(); if (accountPanel) view.append(accountPanel);
    const tools = [];
    if (marketChannel?.mode === 'tencent') tools.unshift(button(`刷新指定渠道：${marketChannel.name}`, 'btn secondary', () => action(async () => { await api('/api/market/refresh', { method: 'POST', body: '{}' }); await loadState(); })));
    tools.push(button('指定信息渠道', 'btn secondary', () => setTab('materials')));
    view.append(sectionHead('行情与渠道', `股价：${marketChannel?.name || '待你指定'} · 交易信息：${channels.trading?.name || '待你指定'}　|　北京时间`, tools));
    if (latest.id && (!marketChannel || latest.source_id !== marketChannel.source_id)) view.append(text('p', '以下为历史保存行情，来自旧渠道；不会自动用于当前账户估值或新研究。', 'source-eval'));
    view.append(quoteRows(latest, watchlist));
    const quality = obj(state.data?.market?.readiness);
    view.append(text('p', `当前渠道行情：${quality.ready_state==='NOT_READY'?'尚未齐备':quality.ready_state==='READY_WITH_GAPS'?'可用，保留缺项':'已就绪'} ${arr(quality.gaps).join('；')}`, 'subtle'));
    if(arr(quality.usable_symbols).length && arr(quality.unavailable_symbols).length) view.append(text('p',`本轮可定量研究：${quality.usable_symbols.join('、')}；${quality.unavailable_symbols.map(x=>x.symbol).join('、')}缺少有效价格，暂不生成定量计划。`,'notice'));
    view.append(packetPanel(false));
    view.append(automationPanel());
    view.append(card([metric('当前有效计划', plans.length ? `${plans.length} 条` : '待形成', plans.length ? '已排除过期与被替代的计划' : '点击“运行一轮研究”后生成'), metric('数据交易日', latest.metadata?.quotes?.length ? [...new Set(latest.metadata.quotes.map(quote => quote.trade_date).filter(Boolean))].join('、') : '待确认', '报价所对应的实际交易日'), metric('已发布 / 未完成', `${runs.filter(run => run.decision).length} / ${runs.filter(run => !['published', 'reviewed'].includes(run.status)).length}`, '未完成研究不视作判断'), metric('待复盘', `${runs.reduce((total, run) => total + reviewRows(run).filter(item => item.observation?.status !== 'completed').length, 0) || '待积累'}`, '不把模型概率当统计胜率')], 'grid grid-4'));
    view.append(sectionHead('当前有效计划', '仅展示按当前渠道形成的有效计划；旧版仍可在研究轮次中查看。'));
    if (!plans.length) view.append(empty('待按指定渠道形成新计划', '已有判断保留为历史；渠道选择不会自动改写研究结论或账户持仓。')); else { const grid = document.createElement('div'); grid.className = 'grid grid-2'; plans.forEach(plan => grid.append(planCard(plan))); view.append(grid); }
    view.append(sectionHead('研究轮次', '默认最新优先；查看独立初稿、交叉回应、最终判断与复盘观察。')); view.append(runs.length ? runList(runs) : empty('尚无研究轮次', '尚未有冻结证据的研究记录。')); view.append(sectionHead('观察名单', '仅用于研究范围，不代表购买建议。')); view.append(settingsForm(settings));
    scheduleAutomationPolling();
  }

  function automationPanel() {
    const automation = obj(state.data?.automation), model = obj(automation.model), schedule = obj(automation.schedule);
    const jobs = arr(automation.jobs).slice().sort((a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0));
    const active = jobs.some(job => ['queued', 'running'].includes(job.status));
    const times = arr(schedule.times).length ? arr(schedule.times).map(time => typeof time === 'object' ? display(time.time || time.at || time.label) : display(time)) : ['13:00', '19:00'];
    const scheduleInstalled = schedule.installed === true ? '已安装' : schedule.installed === false ? '未安装' : '未安装 · 状态待确认';
    const scheduleText = `${scheduleInstalled} · ${schedule.enabled === false ? '已暂停 · ' : ''}每日 ${times.join('、')}（北京时间）`;
    const scheduleNote = schedule.note || (schedule.installed === true ? '电脑需保持开机并已登录，定时任务才能运行。' : '定时任务未安装；手动运行仍可使用。');
    const modelName = model.version || model.model || 'DeepSeek';
    const modelId = model.model || 'deepseek-flash';
    const modelState = model.configured === true ? '模型已配置' : model.configured === false ? '模型未配置' : '配置状态待确认';
    const reasoning = model.reasoning_effort ? `推理强度 ${model.reasoning_effort}` : '推理设置待确认';
    const thinking = model.thinking === 'enabled' ? 'thinking 已启用' : model.thinking ? `thinking ${model.thinking}` : 'thinking 设置待确认';
    const contextLength = model.context_length ? `${Number(model.context_length).toLocaleString('zh-CN')} tokens` : '待确认';
    const maxTokens = model.max_tokens ? `${Number(model.max_tokens).toLocaleString('zh-CN')} tokens` : '待确认';
    const section = document.createElement('section'); section.id = 'automation-panel'; section.className = 'automation-panel';
    const runButton = button(active ? '研究进行中' : '运行一轮研究', 'btn primary', () => action(async () => {
      const job = await api('/api/automation/start', { method: 'POST', body: JSON.stringify({ request_id: crypto.randomUUID() }) });
      await loadState();
      notice(job?.id ? `已提交研究轮次：${job.id}` : '已提交研究轮次。');
    }));
    if (active) runButton.disabled = true;
    section.append(sectionHead('研究运行', '独立 DeepSeek 工作台；提交后在这里查看阶段、结果与需要补齐的条件。', [runButton]));
    const summary = document.createElement('div'); summary.className = 'automation-summary';
    summary.append(card([text('div', '模型', 'metric-label'), text('div', modelName, 'metric-value'), text('div', `${modelId} · ${modelState} · ${reasoning} · ${thinking}`, 'metric-note')], 'card automation-stat'));
    summary.append(card([text('div', '上下文与输出', 'metric-label'), text('div', '每次调用独立 1M', 'metric-value'), text('div', `上下文 ${contextLength} · 每次调用输出上限 ${maxTokens}`, 'metric-note')], 'card automation-stat'));
    summary.append(card([text('div', '定时运行', 'metric-label'), text('div', scheduleText, 'metric-value'), text('div', scheduleNote, 'metric-note')], 'card automation-stat'));
    const body = document.createElement('div'); body.className = 'card automation-card'; body.append(summary, text('h3', '最近运行'));
    if (!jobs.length) body.append(empty('尚无自动化运行', schedule.installed === true ? '点击“运行一轮研究”开始；已安装的定时任务会按上方节奏提交新轮次。' : '点击“运行一轮研究”开始；定时任务未安装。'));
    else {
      const list = document.createElement('div'); list.className = 'automation-job-list';
      const phaseLabels = {
        'initial-opportunity': '机会研究初稿',
        'initial-counter': '反证研究初稿',
        'select-evidence': '选取相关资料',
        'challenge-opportunity': '机会研究回应',
        'challenge-counter': '反证研究回应',
        judge: '风险裁决',
        'decision-opportunity': '机会研究读取裁决',
        'decision-counter': '反证研究读取裁决'
      };
      jobs.forEach(job => {
        const row = document.createElement('article'); row.className = `automation-job automation-job-${job.status || 'unknown'}`;
        const info = document.createElement('div'); info.className = 'automation-job-main';
        const status = ({ queued: '排队中', running: '运行中', completed: '已完成', blocked: '已阻塞 · 需补齐后新建', failed: '失败 · 可继续此轮', interrupted: '已中断 · 可继续此轮' })[job.status] || '状态待确认';
        const phase = phaseLabels[job.phase] || job.phase || '阶段待确认';
        info.append(text('div', `${status} · ${phase}`, 'run-title'), text('div', `${job.id || '无编号'} · 更新 ${fmtTime(job.updated_at)}`, 'run-meta'));
        if (job.run_id) info.append(text('div', `研究轮次：${job.run_id}`, 'run-meta'));
        if (job.error || job.status === 'blocked') info.append(text('p', `${job.status === 'blocked' ? '缺少条件：' : '失败原因：'}${job.error || '接口未返回具体缺口，请检查指定渠道与行情资料。'}`, `automation-job-error ${job.status === 'blocked' ? 'blocked' : ''}`));
        if (job.status === 'blocked') info.append(text('p', '这是该次启动时的缺项记录，后续资料不会改写它。同步任务会在输入就绪后补跑最近被阻断的时段；也可手动开启新轮。', 'subtle'));
        if (job.status === 'completed' && job.run_id) info.append(text('p', '本轮已完成；当前决策主计划未自动重绘，可刷新当前决策查看更新。', 'subtle'));
        const actions = document.createElement('div'); actions.className = 'automation-job-actions';
        if (['failed', 'interrupted'].includes(job.status) && job.id) actions.append(button('继续此轮', 'btn secondary', () => action(async () => { await api(`/api/automation/jobs/${encodeURIComponent(job.id)}/resume`, { method: 'POST', body: '{}' }); await loadState(); notice('已提交继续此轮请求。'); })));
        if (job.status === 'completed' && job.run_id) actions.append(button('查看本轮结果', 'btn secondary', () => action(async () => { const fullRun = await api(`/api/runs/${encodeURIComponent(job.run_id)}`); const discussion = await api(`/api/runs/${encodeURIComponent(job.run_id)}/discussion`); showModal(`研究轮次 ${job.run_id}`, runDetail(fullRun, discussion)); })));
        row.append(info, actions); list.append(row);
      });
      body.append(list);
    }
    section.append(body); return section;
  }

  function planCard(item) {
    const plan = item, element = document.createElement('article');
    const zeroCost = plan.research_cost_mode === 'zero_friction';
    element.className = `card plan-card ${plan.action === 'observe' ? 'observe' : ''} action-${plan.action || 'unknown'}`;
    const top = document.createElement('div'); top.className = 'plan-top';
    const name = plan.name || arr(state.data?.settings?.watchlist).find(stock => stock.symbol === plan.symbol)?.name || '';
    const actionLabels = { buy: '建议买入', reduce: '建议减仓', sell: '建议卖出', avoid: '暂不买入', hold: '继续持有', observe: '观察' };
    top.append(text('div', `${plan.symbol || '未指定'} ${name}`, 'symbol'), text('span', actionLabels[plan.action] || '待确认', 'plan-action'));
    element.append(top, text('p', plan.thesis || '暂无判断摘要'));
    if (plan.decision_basis) element.append(text('p', `决策依据：${plan.decision_basis}`, 'plan-lead'));
    if (plan.long_term_view) element.append(text('p', `长期视角：${plan.long_term_view}`, 'plan-lead'));
    element.append(text('p', `复核收盘：${arr(plan.review_points).map(point => fmtDate(point.review_date)).join('、') || '待确定'}`, 'review-dates'));
    const direction = ({up:'上涨',down:'下跌',flat:'平稳',uncertain:'尚不确定'})[plan.expected_direction] || '待确认';
    const accountQuantity = plan.quantity == null ? '' : `　账户执行数量：${num(plan.quantity, 0)} 股/份`;
    element.append(text('p', `方向：${direction}　期限：${plan.horizon_days ? `${plan.horizon_days} 个交易日` : '待声明'}${accountQuantity}`));
    if (obj(plan.sizing).scenarios?.length || plan.sizing?.basis || arr(plan.sizing?.assumptions).length) element.append(sizingPanel(plan.sizing, zeroCost));
    if (plan.execution) {
      element.append(text('p', `计划生效：${plan.valid_from} 至 ${plan.valid_until} · ${plan.execution.strategy_key}`, 'subtle'));
      const rules = document.createElement('details'); rules.append(text('summary', '查看事前执行条件与概率敏感性'));
      arr(plan.execution.legs).forEach(leg => rules.append(text('p', `${leg.id} · ${actionLabels[leg.action] || leg.action} ${leg.quantity}股/份；条件 ${leg.entry.kind}${leg.entry.price == null ? '' : ' '+leg.entry.price+'元'}；目标 ${leg.take_profit ?? '无'}，止损 ${leg.stop_loss ?? '无'}，${leg.exit_basis === 'close' ? '收盘触发后次日开盘' : '盘中触价'}；${leg.reasoning}`)));
      arr(plan.scenario_metrics).forEach(m => rules.append(text('p', zeroCost ? `${m.label}：二分支盈亏平衡概率 ${num(m.breakeven_probability_zero_cost * 100)}%；假设期望 ${money(m.estimated_ev)}。概率依据：${m.probability_basis}。${m.interpretation}` : `${m.label}：二分支盈亏平衡概率（零费用 / 估计费用）${num(m.breakeven_probability_zero_cost * 100)}% / ${num(m.breakeven_probability_with_cost * 100)}%；假设期望 ${money(m.estimated_ev)}。概率依据：${m.probability_basis}。${m.interpretation}`, 'subtle')));
      if (plan.edge) rules.append(text('p', `获利机制：${plan.edge.mechanism}`), text('p', `竞争解释：${plan.edge.alternative_explanation}`), text('p', `价格已反映程度：${plan.edge.priced_in}`), text('p', `反证条件：${plan.edge.falsifier}`));
      element.append(rules);
    }
    const rid = plan.run?.id || plan.run_id;
    if (rid) element.append(button('查看触发、执行与收益', 'btn secondary', () => action(async () => {
      const report = await api(`/api/runs/${encodeURIComponent(rid)}/outcomes`);
      showModal('条件计划与执行结果', outcomesPanel(report.outcomes));
    })));
    const detail = document.createElement('details'); detail.append(text('summary', '展开触发条件、风险与证据'));
    if (plan.quantity == null) detail.append(text('p', '账本执行量尚未核定；数量方案及所用假设见研究测算。', 'subtle'));
    [['触发条件', plan.trigger], ['失效条件', plan.invalidation], ['收益空间', plan.upside], ['下行风险', plan.downside], ...(!zeroCost ? [['成本影响', plan.costs]] : []), ['反证', arr(plan.counterevidence).join('；')], ['未知', arr(plan.unknowns).join('；')]].forEach(([label, value]) => detail.append(text('h3', label), text('p', display(value))));
    detail.append(text('h3', '本轮冻结证据'));
    arr(plan.evidence_ids).forEach(id => {
      const label = arr(state.data?.evidence).find(source => source.id === id)?.title || id;
      detail.append(button(label, 'btn secondary', () => action(async () => {
        const run = await api(`/api/runs/${encodeURIComponent(item.run.id)}`);
        const source = arr(run.evidence).find(evidence => evidence.id === id);
        if (!source) throw new Error('本轮冻结证据缺失');
        const annotations = arr(state.data?.evidence).find(evidence => evidence.id === id)?.warnings || [];
        showModal(label, evidenceDetail(source, annotations));
      })));
    });
    detail.append(text('p', `替代关系：${plan.supersedes || '首次版本'}`, 'subtle')); element.append(detail);
    const foot = document.createElement('div'); foot.className = 'plan-foot';
    foot.append(text('span', `版本 ${item.run.id || '—'}`), text('span', `发布 ${fmtTime(item.run.decision?.published_at || item.run.created_at)}`));
    element.append(foot); return element;
  }

  function scheduleAutomationPolling() {
    if (state.automationTimer) { clearTimeout(state.automationTimer); state.automationTimer = null; }
    if (state.tab !== 'decision' || !arr(state.data?.automation?.jobs).some(job => ['queued', 'running'].includes(job.status))) return;
    state.automationTimer = setTimeout(async () => {
      state.automationTimer = null;
      if (state.tab !== 'decision') return;
      try {
        const data = await api('/api/state'); state.data = data; state.csrf = obj(data.system).csrf_token || state.csrf;
        const panel = $('#automation-panel'); if (panel) panel.replaceWith(automationPanel());
      } catch (error) {
        notice(error.message, 'error');
      } finally {
        scheduleAutomationPolling();
      }
    }, 15000);
  }

  function sizingPanel(sizing, zeroCost = false) {
    const panel = document.createElement('section'); panel.className = 'sizing-panel';
    panel.append(text('h3', '数量与得失测算'));
    if (sizing?.basis) panel.append(text('p', `测算依据：${sizing.basis}`, 'sizing-basis'));
    const assumptions = arr(sizing?.assumptions);
    if (assumptions.length) {
      panel.append(text('h4', '测算假设'));
      const list = document.createElement('ul'); list.className = 'sizing-assumptions'; assumptions.forEach(assumption => list.append(text('li', assumption))); panel.append(list);
    }
    const scenarios = arr(sizing?.scenarios);
    if (scenarios.length) {
      panel.append(text('h4', '情景测算'));
      const grid = document.createElement('div'); grid.className = 'sizing-scenarios';
      scenarios.forEach(scenario => {
        const quantity = Number(scenario.quantity), entry = Number(scenario.entry_price), exit = Number(scenario.exit_price), stop = Number(scenario.stop_price), fees = Number(scenario.estimated_fees);
        const gross = Number.isFinite(quantity) && Number.isFinite(entry) && Number.isFinite(exit) ? (exit - entry) * quantity : null;
        const net = gross == null || !Number.isFinite(fees) ? null : gross - fees;
        const stopGross = Number.isFinite(quantity) && Number.isFinite(entry) && Number.isFinite(stop) ? (stop - entry) * quantity : null;
        const stopNet = stopGross == null || !Number.isFinite(fees) ? null : stopGross - fees;
        const card = document.createElement('article'); card.className = 'sizing-scenario';
        card.append(text('strong', scenario.label || '未命名情景'), text('p', `研究测算数量：${Number.isInteger(quantity) && quantity > 0 ? num(quantity, 0) : '待确认'} 股/份`));
        card.append(text('p', `参考价（元）：${Number.isFinite(entry) ? num(entry, 3) : '待确认'}　目标价（元）：${Number.isFinite(exit) ? num(exit, 3) : '待确认'}　止损价（元）：${Number.isFinite(stop) ? num(stop, 3) : '待确认'}`));
        card.append(text('p', zeroCost ? `目标价情景：${gross == null ? '待确认' : money(gross)}（按价差测算）` : `毛收益：${gross == null ? '待确认' : money(gross)}　减预估费用：${Number.isFinite(fees) ? money(fees) : '待确认'}　净情景${net != null && net < 0 ? '损失' : '收益'}：${net == null ? '待确认' : money(net)}`, `sizing-result ${net != null && net < 0 ? 'loss' : ''}`));
        card.append(text('p', zeroCost ? `止损价情景：${stopGross == null ? '待确认' : money(stopGross)}` : `止损价结果（毛收益减预估费用）：${stopNet == null ? '待确认' : money(stopNet)}`, `sizing-stop ${stopNet != null && stopNet < 0 ? 'loss' : ''}`));
        if (scenario.reasoning) card.append(text('p', `推理：${scenario.reasoning}`, 'sizing-reasoning'));
        grid.append(card);
      });
      panel.append(grid);
    }
    return panel;
  }

  function runList(runs) {
    const wrap = document.createElement('div'); wrap.className = 'run-list'; runs.slice().sort((a, b) => new Date(b.created_at || b.as_of) - new Date(a.created_at || a.as_of)).forEach(run => { const row = document.createElement('div'); row.className = 'run-row'; const info = document.createElement('div'); info.append(text('div', `${run.mode === 'replay' ? '回放' : '实时'}研究 · ${fmtDate(run.as_of)} · ${arr(run.symbols).join(' / ')}`, 'run-title'), text('div', `${fmtTime(run.created_at || run.as_of)}　${({published:'已发布',reviewed:'已复核',prepared:'已冻结'})[run.status] || '研究中'}`, 'run-meta')); row.append(info, button('查看轮次', 'btn secondary', () => action(async () => { const fullRun = await api(`/api/runs/${encodeURIComponent(run.id)}`); const discussion = await api(`/api/runs/${encodeURIComponent(run.id)}/discussion`); showModal(`研究轮次 ${run.id}`, runDetail(fullRun, discussion)); }))); wrap.append(row); }); return wrap;
  }

  function runDetail(run, discussion = null) {
    const wrap = document.createElement('div'), opinions = obj(run.opinions);
    if (run.channel_review_required) wrap.append(text('p', '历史裁决：尚未按当前用户指定渠道重新核对，不作为当前执行建议。原始内容保持不变。', 'source-eval'));
    const payload = value => obj(value?.payload || value);
    const stages = [['机会研究初稿', payload(opinions.opportunity?.initial)], ['反证研究初稿', payload(opinions.counter?.initial)], ['机会研究回应', payload(opinions.opportunity?.challenge)], ['反证研究回应', payload(opinions.counter?.challenge)], ['最终判断', run.decision]];
    if (discussion) wrap.append(sharedDiscussion(discussion));
    else stages.forEach(([title, value]) => { const block = document.createElement('div'); block.className = 'detail'; const item = obj(value); block.append(text('h3', title), text('p', item.summary || item.reasoning || '尚无内容', 'method')); ['claims', 'evidence_ids', 'counterevidence', 'unknowns', 'changes'].forEach(field => { if (arr(item[field]).length) block.append(text('p', `${({claims:'判断',evidence_ids:'证据',counterevidence:'反证',unknowns:'未知',changes:'观点变化'})[field]}：${item[field].join('；')}`, 'subtle')); }); wrap.append(block); });
    if (run.decision?.reasoning) wrap.append(text('h3', '裁决依据与争议处理'), text('p', run.decision.reasoning, 'method'));
    arr(run.decision?.plans).forEach(plan => wrap.append(text('h3', '最终计划明细'), planCard({ ...plan, run })));
    const reviewItems = reviewRows(run);
    wrap.append(text('h3', '预定复核与实际结果'), text('p', '这是判断的事后复盘日程：到预定交易日收盘后，用实际行情核对原来的方向与计划。待核对表示尚未到期或行情待补；这些日期不是下单提醒，也不代表真实成交。', 'subtle'));
    reviewItems.forEach(item => wrap.append(text('div', `${item.symbol || '未指定'} · ${fmtDate(item.review_date)} · ${item.observation?.status === 'completed' ? '已核对' : '待核对'} · ${item.observation?.return_pct == null ? '结果尚未产生' : num(item.observation.return_pct) + '%'}`, 'evidence-text')));
    return wrap;
  }

  function sharedDiscussion(discussion) {
    const section = document.createElement('section');
    section.className = 'discussion-section';
    section.append(text('h3', '共享讨论视图'));
    const note = document.createElement('div');
    note.className = 'discussion-note';
    note.append(text('strong', discussion.shared_initials ? '初稿已进入共享讨论' : '独立初稿阶段保持隔离'));
    note.append(text('p', discussion.shared_initials ? '共享阶段可查看双方已提交的意见。' : '机会研究与反证研究的独立初稿不会在初稿阶段互相可见；交叉质疑阶段才共享。'));
    if (discussion.delivery === 'pull') note.append(text('p', `这是按需读取的讨论视图：${discussion.delivery_note || '不表示实时推送、已读或接受。'}`, 'subtle'));
    section.append(note);
    const messages = arr(discussion.messages);
    if (!messages.length) {
      section.append(empty('当前没有共享消息', '共享讨论只投影已经保存的意见与裁决，不生成新的发言。'));
      return section;
    }
    const stream = document.createElement('div');
    stream.className = 'discussion-stream';
    const names = { opportunity: '机会研究', counter: '反证研究', judge: '风险裁决' };
    const phases = { initial: '独立初稿', challenge: '交叉质疑回应', decision: '最终裁决' };
    const messageNames = new Map(messages.map(message => [message.id, `${names[message.sender] || message.sender} · ${phases[message.phase] || message.phase}`]));
    messages.forEach(message => {
      const item = document.createElement('article');
      item.className = 'discussion-message';
      const header = document.createElement('div');
      header.className = 'discussion-message-head';
      header.append(text('strong', names[message.sender] || message.sender || '未知角色'));
      header.append(text('span', `${phases[message.phase] || message.phase || '阶段待确认'} · ${fmtTime(message.created_at)}`, 'discussion-meta'));
      item.append(header);
      const recipients = arr(message.recipients).map(role => names[role] || role).join('、');
      item.append(text('p', recipients ? `收件角色：${recipients}` : '收件角色：待确认', 'discussion-meta'));
      if (arr(message.reply_to).length) item.append(text('p', `回应：${message.reply_to.map(id => messageNames.get(id) || id).join('；')}`, 'discussion-meta'));
      const payload = obj(message.payload);
      item.append(text('p', payload.summary || payload.reasoning || '这条消息没有摘要。', 'method'));
      const detail = document.createElement('details');
      detail.append(text('summary', '展开判断、证据与观点变化'));
      [['判断', payload.claims], ['证据', payload.evidence_ids], ['反证', payload.counterevidence], ['未知', payload.unknowns], ['观点变化', payload.changes]].forEach(([label, value]) => {
        if (arr(value).length) detail.append(text('p', `${label}：${value.join('；')}`, 'subtle'));
      });
      if (arr(payload.forecasts).length) detail.append(forecastPanel(payload.forecasts));
      if (message.source_ref) detail.append(text('p', `原始记录：${message.source_ref}`, 'discussion-meta'));
      item.append(detail);
      stream.append(item);
    });
    section.append(stream);
    return section;
  }

  function reviewRows(run) {
    const latest = new Map();
    arr(run.reviews).forEach(review => arr(review.observations).forEach(item => {
      const key = `${item.symbol}:${item.review_date}`;
      if (latest.get(key)?.status !== 'completed') latest.set(key, item);
    }));
    const unique = new Map();
    arr(run.decision?.plans).forEach(plan => arr(plan.review_points).forEach(point => {
      const key = `${plan.symbol}:${point.review_date}`;
      unique.set(key, {...point, symbol:plan.symbol, observation:latest.get(key)});
    }));
    return [...unique.values()].sort((a,b) => a.review_date.localeCompare(b.review_date));
  }

  function settingsForm(settings) {
    const form = document.createElement('form'); form.className = 'card form-card'; const list = document.createElement('div'); list.className = 'field full'; list.append(text('label', '标的')); const rows = document.createElement('div'); arr(settings.watchlist).forEach(item => addWatchRow(rows, item)); if (!rows.children.length) addWatchRow(rows); list.append(rows, button('增加标的', 'btn secondary', () => addWatchRow(rows))); form.append(list, inputField('最大可承受亏损金额（元，可留空）', 'risk_budget', 'number', { step: '0.01', min: '0', value: settings.risk_budget ?? '' }), formActions('保存观察名单'));
    form.addEventListener('submit', event => { event.preventDefault(); action(async () => { const watchlist = $$('[data-watch-row]', rows).map(row => ({ symbol: $('[name=symbol]', row).value.trim(), name: $('[name=name]', row).value.trim() })).filter(item => item.symbol); await api('/api/settings', { method: 'PUT', body: JSON.stringify({ watchlist, risk_budget: form.elements.risk_budget.value ? Number(form.elements.risk_budget.value) : null }) }); await loadState(); }); }); return form;
  }

  function addWatchRow(root, value = {}) { const row = document.createElement('div'); row.dataset.watchRow = 'true'; row.className = 'position-table'; row.append(input('symbol', '代码', value.symbol || '', true), input('name', '名称（可选）', value.name || ''), button('删除', 'btn danger', () => row.remove())); root.append(row); }

  function renderRoles() {
    const view = $('#view-roles'); view.replaceChildren(); const roles = arr(state.data?.roles); if (!state.role) state.role = roles[0]?.id || 'opportunity'; view.append(sectionHead('角色与记忆', '每个角色拥有独立方法与连续记忆；页面只展示已保存内容。')); const switcher = document.createElement('div'); switcher.className = 'role-switcher'; roles.forEach(role => switcher.append(button(role.name || role.id, state.role === role.id ? 'active' : '', () => { state.role = role.id; renderRoles(); }))); view.append(roles.length ? switcher : empty('尚未加载角色', '角色方法由项目 roles/ 提供。')); const role = roles.find(item => item.id === state.role); if (!role) return; if (role.agent) view.append(card([text('h3', role.agent.name), text('p', `独立 Agent：${role.agent.id} · 共用 DeepSeek API · ${role.agent.model?.model || 'deepseek-flash'} / max`), text('p', `独立会话：${role.agent.session_count || 0} 轮 · 独立方法与结果记忆；初稿阶段不读取对方本轮意见。`, 'subtle')])); const panel = document.createElement('div'); panel.className = 'grid grid-2'; panel.append(card([text('h3', role.name || role.id), text('p', (role.method || '方法模板待提供。').replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, '').trim(), 'method')], 'card role-panel')); const memory = card([text('h3', `复盘经验 · ${arr(role.memory).length} 条`)], 'card role-panel'); if (typeof role.memory === 'string') memory.append(text('div', role.memory, 'method')); else if (arr(role.memory).length) role.memory.forEach(item => { const facts = arr(item.facts).map(fact => `${fact.symbol}：${fact.review_date}，价格变化 ${num(fact.return_pct)}%，引用 ${arr(fact.source_ids).join('、')}`).join('；'); const reflection = obj(item.reflection).text || ''; memory.append(card([text('strong', `${item.run_id || '无 run_id'} · ${item.review_id || '无 review_id'}`), text('p', `事实：${facts || '—'}`), text('p', `反思：${reflection || '—'}`), assessmentPanel(item)], 'memory-item')); }); else memory.append(text('p', '尚无已保存记忆；未复核的结论不会自动写入。', 'subtle')); panel.append(memory); view.append(panel);
    const history = arr(state.data?.runs);
    view.append(sectionHead('历史意见与原始轮次', '选择轮次查看各角色当时的独立意见、交叉回应及其证据；尚未复盘的判断保留在原轮次中。'));
    view.append(history.length ? runList(history) : empty('尚无研究历史', '完成研究后保留当时的意见，未来结果到期后再写入复盘经验。'));
  }

  function forecastPanel(forecasts) {
    const box=document.createElement('div'),direction={up:'上涨',down:'下跌',flat:'平稳',uncertain:'未确定'};
    arr(forecasts).forEach(f=>box.append(text('p',`${f.symbol} · ${f.horizon_days}交易日 · 事前 ${direction[f.expected_direction]||f.expected_direction}；${f.status?({pending:'未到期/缺结果',abstained:'未给确定方向',scored:f.direction_hit?'方向命中':'方向未命中'}[f.status]||f.status):'等待结果'}。依据：${f.rationale}；反证：${f.falsifier}`)));
    return box;
  }

  function assessmentPanel(memory) {
    const d=document.createElement('details');d.append(text('summary','自己的事前判断、裁决回应与执行结果'));
    const a=obj(memory.own_assessment);
    Object.entries(obj(a.own_opinions)).forEach(([phase,p])=>d.append(text('h4',phase==='initial'?'独立初稿':'交叉回应'),text('p',p.summary),forecastPanel(p.forecasts)));
    if(a.judge_response)d.append(text('p',`裁决对本角色的处理：${a.judge_response}`));
    if(a.decision_reasoning)d.append(text('p',`原裁决依据：${a.decision_reasoning}`));
    d.append(outcomesPanel(memory.plan_outcomes));return d;
  }

  function renderMaterials() {
    const view = $('#view-materials');
    view.replaceChildren();
    view.append(sectionHead('资料与来源', '先由你指定获取渠道；已保存资料可继续查阅。'));
    view.append(channelForm());
    view.append(packetPanel(true));
    const forms = document.createElement('div');
    forms.className = 'grid grid-2';
    forms.append(textForm(), fileForm());
    if (state.data?.settings?.channels?.market) view.append(marketInputForm());
    if (state.data?.settings?.channels?.caixin_enabled) view.append(caixinSourcePanel());
    const ratings = document.createElement('details');
    ratings.append(text('summary', '来源分项评价：事实、时效与预测关联'), text('p','三项分开记录；股价涨跌不决定报道是否真实。栏目与期次沿用清洗层标注，尚未逐条核验。'), sourceBreakdown(arr(state.data?.sources)));
    view.append(ratings);
    const manual = document.createElement('details');
    manual.append(text('summary','手动补充文本或 PDF'), forms);
    view.append(manual);
    view.append(sectionHead('已保存资料', '按关键词、来源、栏目、来源标注期次和提取状态筛选；每页最多 25 条。'));
    view.append(materialSearchPanel());
  }

  function packetPanel(withImport) {
    const value = obj(state.data?.packet_status), section = document.createElement('section'); section.className = 'card';
    const local = String(state.data?.settings?.channels?.trading?.reference || '').startsWith('codex-automation:');
    const labels={READY:'正文已接收',READY_WITH_GAPS:'正文已接收，附使用限制',NOT_READY:'等待来源正文'};
    section.append(text('h3', `情报交付：${labels[value.ready_state] || '待核对'}`));
    if(value.evidence_id) section.append(text('p',`${local?(value.source_role==='primary'?'本地采集主包 · ':'网页补充包 · '):''}已接收${value.session==='close'?'盘后':value.session==='midday'?'午间':'非交易时段'}包 · 数据截至 ${fmtTime(value.as_of)} · 导入 ${fmtTime(value.imported_at)}`));
    section.append(text('p','研究使用价格、相关事实和近期事件。定量计划按各标的有效价格生成；辅助行情缺失只限制依赖它的论据。','subtle'));
    if(value.evidence_id) section.append(button('阅读已收到的情报原文','btn secondary',()=>action(async()=>showModal('已收到的情报原文',evidenceDetail(await api(`/api/evidence/${encodeURIComponent(value.evidence_id)}`))))));
    const checks=document.createElement('details');checks.append(text('summary','查看具体使用限制与源端说明'));
    arr(value.upstream_gaps).forEach(g=>checks.append(text('p',`原包说明：${g}`,'subtle')));
    arr(value.gaps).forEach(g => checks.append(text('p',g,'notice')));
    arr(value.warnings).forEach(g => checks.append(text('p',g,'subtle')));section.append(checks);
    section.append(text('p', local ? '本地 Luna/max 主动采集行情、公告和事件；网页情报用于补充线索与交叉核对，未更新不阻断本地主包交付。' : 'Codex 定时同步已绑定的情报对话。迟到的正文和有效价格到达后，会补跑此前等待资料的时段。', 'subtle'));
    if (withImport) {
      section.append(button('查看并复制源端交付提示词','btn secondary',()=>action(async()=>{const t=await api('/api/packets/template'),body=document.createElement('div'),field=inputField('完整交付提示词','handoff_prompt','textarea',{rows:'15',readonly:'true'});field.querySelector('textarea').value=t.prompt;body.append(text('p','将下列内容用于已指定的情报对话。这里只展示交付合同，不会代你向远端发送消息。'),field,button('复制提示词','btn secondary',()=>action(async()=>{if(!navigator.clipboard)throw new Error('当前浏览器无法写剪贴板，请选中文字复制。');await navigator.clipboard.writeText(t.prompt);notice('已复制，也可在文本框内全选后手动复制。');})));showModal('源端交付提示词',body);})));
      const detail = document.createElement('details'); detail.append(text('summary','导入结构情报包'));
      const form = document.createElement('form'); form.append(inputField('情报包 JSON', 'packet','textarea',{rows:'10',required:'true'}),formActions('核对并导入'));
      form.addEventListener('submit', e => {e.preventDefault();action(async()=>{
        const p=JSON.parse(form.elements.packet.value);p.request_id=p.request_id||requestId(form);
        await api('/api/packets/import',{method:'POST',body:JSON.stringify(p)});await loadState();
      });}); detail.append(form); section.append(detail);
    }
    return section;
  }

  function outcomesPanel(outcomes) {
    const panel=document.createElement('div');
    const labels={terminated_counterfactual:'已失效或替代，仅作原规则反事实',expired_pending_unfilled:'期限结束，次日单未成交',pending:'等待完整结果',legacy_unstructured:'历史自然语言计划，不能事后补规则评分',unresolved:'路径或成交尚不确定',expired:'计划到期',evaluated:'已有阶段结果',not_triggered:'未触发',closed:'模拟已退出',held:'模拟持有中',path_ambiguous:'同日止盈止损先后未知',fill_unknown:'成交可行性未知',needs_manual_confirmation:'等待人工条件核对',expired_untriggered:'到期未触发',expired_marked:'到期持仓估值',stayed_in_cash:'保持现金',triggered_pending_next_open:'已触发，等待次日开盘'};
    arr(outcomes).forEach(o=>{
      const block=card([text('h3',`${o.symbol} · ${labels[o.status]||o.status}`),text('p',o.reason||o.basis,'subtle')]);
      block.append(text('p',o.research_cost_mode === 'zero_friction' ? `按条件回放的价格损益 ${money(o.gross_pnl)}；相对事前对照 ${money(o.incremental_estimated_pnl)}` : `按条件回放的价格损益 ${money(o.gross_pnl)}；扣估计交易费用 ${money(o.estimated_net_pnl)}；相对事前对照 ${money(o.incremental_estimated_pnl)}`));
      arr(o.legs).forEach(l=>{block.append(text('p',`${l.leg_id} · ${labels[l.status]||l.status} · ${l.quantity}股/份 · ${arr(l.issues).join('；')}`));arr(l.fills).forEach(f=>block.append(text('p',`${f.date} · ${f.side==='buy'?'模拟买入':'模拟卖出'} ${f.quantity}股/份 × ${num(f.price,3)}；${f.reason}`,'subtle')));});
      ['actual','paper'].forEach(mode=>{const a=obj(o.execution_records?.[mode]);block.append(text('h4',mode==='actual'?'人工确认的实际执行':'独立模拟记录'),text('p',`关联成交 ${a.trade_count||0} 笔；相对不执行这些成交 ${money(a.incremental_trade_pnl)}；闭合回合净额 ${money(a.closed_roundtrip_net)}`),text('p',a.scope||'没有执行记录，不能计入真实盈利','subtle'));});
      arr(o.execution_records?.events).forEach(e=>block.append(text('p',`${fmtTime(e.as_of)} · ${e.kind} · ${e.note}`)));
      panel.append(block);
    });
    return panel;
  }

  function channelForm() {
    const channels = obj(state.data?.settings?.channels), market = obj(channels.market), trading = obj(channels.trading);
    const form = document.createElement('form'); form.className = 'card form-card';
    form.append(text('h3', '由你指定的信息渠道'), text('p', '未指定时不联网获取、不自动替换来源。人工提供可填写你使用的平台或文件来源，资料按该来源录入。', 'subtle'));
    if (/^https:\/\/chatgpt\.com\/c\/[a-f0-9-]+$/.test(trading.reference || '')) {
      form.append(text('p', `已固定对话来源：${trading.name}。Codex负责同步已登记的实际输出对话；情报原文可读与完整交易计划可生成是两个状态。`, 'source-eval'));
    }
    if (String(trading.reference || '').startsWith('codex-automation:')) {
      form.append(text('p', '本地采集为主，网页情报为辅。行情直接取指定供应商；公告和事件保留实际原文来源，网页内容不覆盖主价格。', 'source-eval'));
    }
    const fields = document.createElement('div'); fields.className = 'form-grid';
    const mode = selectField('股价获取方式', 'market_mode', [['', '暂不指定'], ['manual', '按指定渠道人工提供'], ['tencent', '腾讯行情（现有在线适配器）']]);
    mode.querySelector('select').value = market.mode || '';
    fields.append(mode, inputField('股价渠道名称', 'market_name', 'text', {value: market.name || '', placeholder: '你使用的平台或文件来源'}),
      inputField('股价渠道网址或文件说明', 'market_reference', 'text', {value: market.reference || ''}),
      inputField('交易信息渠道名称', 'trading_name', 'text', {value: trading.name || '', placeholder: '由你指定；不连接券商下单'}),
      inputField('交易信息网址或文件说明', 'trading_reference', 'text', {value: trading.reference || ''}));
    const caixin = document.createElement('label'); caixin.className = 'channel-check full';
    const check = document.createElement('input'); check.type = 'checkbox'; check.name = 'caixin_enabled'; check.checked = Boolean(channels.caixin_enabled);
    caixin.append(check, text('span', '保留本地财新归档作为研究资料渠道（仅同步已有文件）')); fields.append(caixin);
    form.append(fields, formActions('保存我指定的渠道'));
    form.addEventListener('submit', event => { event.preventDefault(); action(async () => {
      const data = new FormData(form), mode = data.get('market_mode'), marketName = String(data.get('market_name')).trim(), tradingName = String(data.get('trading_name')).trim();
      if (mode === 'manual' && !marketName) throw new Error('请填写你指定的股价渠道名称');
      await api('/api/settings', {method:'PUT', body:JSON.stringify({channels:{
        market: mode ? {mode, name: marketName || '腾讯行情', reference: data.get('market_reference') || ''} : null,
        trading: tradingName ? {mode:'manual', name:tradingName, reference:data.get('trading_reference') || '', additional_references: data.get('trading_reference') === trading.reference ? arr(trading.additional_references) : []} : null,
        caixin_enabled: check.checked
      }})});
      await loadState(); notice('已保存你指定的渠道；旧研究保留，下一轮按新渠道核对。');
    }); });
    return form;
  }

  function marketInputForm() {
    const channel = state.data?.settings?.channels?.market;
    const detail = document.createElement('details'), form = document.createElement('form'); form.className = 'card form-card';
    detail.append(text('summary', `录入来自「${channel.name}」的报价`));
    form.append(text('p', '请保留原始渠道的报价时间；不会把录入时间当行情时间。日线或批量资料可按同一渠道整理导入。', 'subtle'));
    const fields = document.createElement('div'); fields.className = 'form-grid';
    fields.append(inputField('证券代码', 'symbol', 'text', {required:'true', pattern:'[056][0-9]{5}'}),
      inputField('名称', 'name'), inputField('价格（元）', 'price', 'number', {required:'true', min:'0.001', step:'0.001'}),
      inputField('原始报价时间（北京时间）', 'as_of', 'datetime-local', {required:'true'}),
      selectField('报价状态', 'phase', [['intraday','盘中'],['closed','已收盘']]));
    ['previous_close','open','high','low','volume','amount'].forEach((k,i)=>fields.append(inputField(['前收（元）','开盘（元）','最高（元）','最低（元）','成交量','成交额（人民币元）'][i],k,'number',{required:'true',min:'0',step:'any'})));
    fields.append(selectField('成交量单位','volume_unit',[['shares','股/份'],['lots','手']]),inputField('原始价格来源','price_source','text',{required:'true',value:channel.reference||channel.name}),selectField('数据时效','data_timing',[['closing_snapshot','正式收盘'],['delayed','延迟'],['realtime','实时']]));
    form.append(fields, formActions('保存指定渠道报价'));
    form.addEventListener('submit', event => { event.preventDefault(); action(async () => {
      const data = new FormData(form);
      await api('/api/market/import', {method:'POST', body:JSON.stringify({request_id:requestId(form), quotes:[{
        symbol:data.get('symbol'), name:data.get('name') || data.get('symbol'), price:Number(data.get('price')),
        as_of:beijingIso(data.get('as_of')),price_as_of:beijingIso(data.get('as_of')), phase:data.get('phase'),...Object.fromEntries(['previous_close','open','high','low','volume','amount'].map(k=>[k,Number(data.get(k))])),volume_unit:data.get('volume_unit'),amount_unit:'CNY',price_source:data.get('price_source'),data_timing:data.get('data_timing')
      }]})}); await loadState(); notice('已保存指定渠道报价，未改变持仓或旧判断。');
    }); }); detail.append(form);
    const bulk=document.createElement('form');bulk.append(inputField('批量行情 JSON（quotes 与 daily）','json','textarea',{required:'true',rows:'8'}),formActions('导入报价与历史日线'));
    bulk.addEventListener('submit',e=>{e.preventDefault();action(async()=>{const v=JSON.parse(bulk.elements.json.value);v.request_id=v.request_id||requestId(bulk);await api('/api/market/import',{method:'POST',body:JSON.stringify(v)});await loadState();});});detail.append(bulk);return detail;
  }

  function caixinSourcePanel() {
    const panel = document.createElement('section');
    panel.className = 'caixin-panel';
    const heading = sectionHead('财新长期本地来源', '同步已存在的本地 ingestion 资料；不会在页面发起网络抓取。');
    const content = document.createElement('div');
    content.className = 'caixin-content';
    const syncForm = document.createElement('form');
    syncForm.className = 'caixin-sync';
    syncForm.append(button('同步本地财新资料', 'btn primary', null, { type: 'submit' }));
    syncForm.addEventListener('submit', event => {
      event.preventDefault();
      action(async () => {
        const result = await api('/api/sources/caixin/sync', { method: 'POST', body: JSON.stringify({ request_id: requestId(syncForm) }) });
        state.caixinLastSync = result;
        syncForm.dataset.requestId = '';
        await loadState();
      });
    });
    panel.append(heading, syncForm, content);
    loadCaixinStatus(content);
    return panel;
  }

  async function loadCaixinStatus(content) {
    content.replaceChildren(text('p', '正在读取本地财新来源状态…', 'subtle'));
    try {
      const status = await api('/api/sources/caixin');
      state.caixinStatus = status;
      renderCaixinStatus(content, status);
      if (state.caixinLastSync) renderCaixinSyncResult(content, state.caixinLastSync);
    } catch (error) {
      content.replaceChildren(text('p', `财新来源状态读取失败：${error.message}`, 'source-eval'));
      if (state.caixinLastSync) renderCaixinSyncResult(content, state.caixinLastSync);
    }
  }

  function renderCaixinStatus(content, status) {
    const summary = document.createElement('div');
    summary.className = 'caixin-summary';
    summary.append(
      metric('当前快照资料', status.total ?? '待确认', '最近一次来源快照'),
      metric('当前快照可用', status.ready ?? '待确认', '最近一次来源快照'),
      metric('当前快照受限', status.blocked ?? '待确认', '最近一次来源快照'),
      metric('最新公开日期', status.latest_published_at ? fmtDate(status.latest_published_at) : '待确认', '来源公开时间'),
      metric('最近同步', status.last_sync ? fmtTime(status.last_sync) : '尚未同步', '本地同步记录')
    );
    const detail = document.createElement('div');
    detail.className = 'caixin-detail';
    detail.append(caixinObjectDetail('当前来源快照', status.source_snapshot), caixinObjectDetail('累计版本库', status.library));
    content.replaceChildren(summary, detail);
  }

  function renderCaixinSyncResult(content, result) {
    const block = document.createElement('div');
    block.className = 'caixin-sync-result';
    block.append(text('strong', `同步状态：${connectionLabel(result.status)}`));
    [['扫描', result.scanned], ['新建', result.created], ['已存在', result.existing], ['可用', result.ready], ['受限', result.blocked], ['无效', result.invalid]].forEach(([label, value]) => block.append(text('span', `${label} ${value ?? '待确认'}`)));
    if (result.gaps !== undefined) block.append(text('p', `缺口：${formatCaixinGaps(result.gaps)}。同一篇资料可能有多个问题。`, 'source-eval'));
    content.prepend(block);
  }

  function connectionLabel(value) {
    const status = obj(value);
    return ({ ready: '可用', available: '可用', needs_attention: '需要处理', source_missing: '来源目录缺失' })[status.connection_status] || (typeof value === 'string' ? value : '待确认');
  }

  function formatCaixinGaps(gaps) {
    if (!Array.isArray(gaps)) return gaps == null ? '待确认' : String(gaps);
    if (!gaps.length) return '无';
    return gaps.map(gap => `${gap.label || gap.code || '未命名缺口'}${gap.count == null ? '' : `（${gap.count}）`}`).join('；');
  }

  function caixinObjectDetail(label, value) {
    const detail = document.createElement('details');
    detail.append(text('summary', label));
    const labels = {total:'扫描资料',ready:'可用正文',blocked:'正文受限',invalid:'无法解析',article_count:'不同文章',version_count:'保留版本',ready_versions:'可读版本',blocked_versions:'受限版本'};
    if (value == null) detail.append(text('p', '待确认'));
    else Object.entries(value).forEach(([key, count]) => detail.append(text('p', `${labels[key] || key}：${count}`)));
    return detail;
  }

  const materialSearch = { q: '', source_id: '', category: '', issue: '', status: '', offset: 0, limit: 25, serial: 0 };

  function materialSearchPanel() {
    const panel = document.createElement('div');
    panel.className = 'material-search-panel';
    const form = document.createElement('form');
    form.className = 'card material-search-form';
    const fields = document.createElement('div');
    fields.className = 'form-grid';
    const query = inputField('搜索正文、标题、文章编号或期次', 'materials_q', 'search');
    const source = selectField('来源', 'materials_source', [['', '全部来源'], ...knownSourceOptions()]);
    const category = selectField('来源栏目', 'materials_category', []);
    const issue = selectField('来源标注期次', 'materials_issue', []);
    const status = selectField('提取状态', 'materials_status', [['', '全部状态'], ['ready', '可读'], ['needs_text', '需补文字'], ['error', '获取失败']]);
    query.querySelector('input').value = materialSearch.q;
    source.querySelector('select').value = materialSearch.source_id;
    status.querySelector('select').value = materialSearch.status;
    fields.append(query, source, category, issue, status);
    form.append(fields, formActions('检索'));
    const syncFilters = () => {
      materialSearch.q = query.querySelector('input').value.trim();
      materialSearch.source_id = source.querySelector('select').value;
      materialSearch.category = category.querySelector('select').value;
      materialSearch.issue = issue.querySelector('select').value;
      materialSearch.status = status.querySelector('select').value;
      materialSearch.offset = 0;
      loadMaterialPage(panel, { category, issue, status });
    };
    form.addEventListener('submit', event => { event.preventDefault(); syncFilters(); });
    source.querySelector('select').addEventListener('change', () => {
      category.querySelector('select').value = '';
      issue.querySelector('select').value = '';
      syncFilters();
    });
    [category, issue, status].forEach(field => field.querySelector('select').addEventListener('change', syncFilters));
    const results = document.createElement('div');
    results.className = 'material-search-results';
    panel.append(form, results);
    loadMaterialPage(panel, { category, issue, status });
    return panel;
  }

  async function loadMaterialPage(panel, controls) {
    const serial = ++materialSearch.serial;
    const results = $('.material-search-results', panel);
    if (!results) return;
    results.replaceChildren(card([text('span', '正在检索资料…', 'subtle')], 'loading-panel'));
    const params = new URLSearchParams({ q: materialSearch.q, source_id: materialSearch.source_id, category: materialSearch.category, issue: materialSearch.issue, status: materialSearch.status, offset: String(materialSearch.offset), limit: String(materialSearch.limit) });
    try {
      const payload = await api(`/api/materials/search?${params.toString()}`);
      if (serial !== materialSearch.serial) return;
      if (controls?.category) {
        setSearchOptions(controls.category, payload.categories, '全部栏目');
        setSearchOptions(controls.issue, payload.issues, '全部期次');
        controls.category.querySelector('select').value = materialSearch.category;
        controls.issue.querySelector('select').value = materialSearch.issue;
      }
      const items = arr(payload.items);
      const total = Number(payload.total || 0);
      const list = document.createElement('div');
      list.className = 'source-list';
      if (items.length) items.forEach(item => list.append(materialSearchRow(item)));
      else list.append(empty('没有匹配资料', '可修改筛选词，或等待服务端来源同步。'));
      results.replaceChildren(list, materialPagination(panel, total));
    } catch (error) {
      if (serial !== materialSearch.serial) return;
      results.replaceChildren(empty('资料检索失败', error.message));
    }
  }

  function setSearchOptions(field, values, emptyLabel) {
    if (!values) return;
    const select = field.querySelector('select');
    const current = select.value;
    select.replaceChildren();
    const emptyOption = text('option', emptyLabel); emptyOption.value = ''; select.append(emptyOption);
    arr(values).forEach(value => { const option = text('option', value); option.value = value; select.append(option); });
    if (arr(values).includes(current)) select.value = current;
  }

  function materialPagination(panel, total) {
    const footer = document.createElement('div');
    footer.className = 'material-pagination';
    const page = Math.floor(materialSearch.offset / materialSearch.limit) + 1;
    const pages = Math.max(1, Math.ceil(total / materialSearch.limit));
    footer.append(text('span', `${total} 条资料 · 第 ${page} / ${pages} 页`, 'subtle'));
    const previous = button('上一页', 'btn secondary', () => { materialSearch.offset = Math.max(0, materialSearch.offset - materialSearch.limit); loadMaterialPage(panel, {}); });
    const next = button('下一页', 'btn secondary', () => { materialSearch.offset += materialSearch.limit; loadMaterialPage(panel, {}); });
    previous.disabled = materialSearch.offset <= 0;
    next.disabled = materialSearch.offset + materialSearch.limit >= total;
    footer.append(previous, next);
    return footer;
  }

  function materialSearchRow(item) {
    const row = document.createElement('article');
    row.className = 'source-row material-result';
    const info = document.createElement('div');
    const status = ({ ready: '可读', needs_text: '需补文字', error: '获取失败' })[item.status] || item.status || '状态待确认';
    info.append(text('div', item.title || item.article_id || item.id, 'run-title'));
    info.append(text('div', `${status} · ${item.source_id || '无来源'} · ${item.category || '栏目待确认'} · ${item.issue_label || '期次待确认'}`, 'evidence-meta'));
    if (item.article_id) info.append(text('div', `文章编号：${item.article_id}`, 'evidence-meta'));
    info.append(text('div', `公开：${item.published_at ? (item.published_at_precision === 'date' ? fmtDate(item.published_at) + '（日期精度）' : fmtTime(item.published_at)) : '未确认'}；接入：${fmtTime(item.available_at)}`, 'evidence-meta'));
    if (item.preview) info.append(text('p', item.preview, 'material-preview'));
    if (arr(item.warnings).length) item.warnings.forEach(warning => info.append(text('p', warning, 'source-eval')));
    row.append(info, button('查看段落与页码', 'btn secondary', () => action(async () => showModal(item.title || item.id, evidenceDetail(await api(`/api/evidence/${encodeURIComponent(item.id)}`))))));
    return row;
  }

  function knownSourceOptions() {
    return arr(state.data?.sources).filter(source => source.document_count).map(source => [source.id, source.name || source.id]);
  }
  function sourceBreakdown(sources) {
    const wrap = document.createElement('div'); wrap.className = 'grid grid-2';
    const used = sources.filter(source => source.document_count || arr(source.facts?.checks).length);
    if (!used.length) return empty('来源尚待评价', '导入资料、核验事实和积累前瞻结果后显示分项样本。');
    used.forEach(source => {
      const facts = obj(source.facts), timing = obj(source.timeliness), prediction = obj(source.prediction);
      const box = card([
        text('h3',source.name || source.id),
        text('p',`事实核验：正确 ${facts.correct || 0}，错误 ${facts.incorrect || 0}，待核验 ${facts.unverified || 0}`),
        text('p',`已核验正确比例：${facts.accuracy == null ? '待评' : num(facts.accuracy * 100) + '%'}。只评价明确核验过的主张。`,'subtle'),
        text('p',timing.samples ? `新闻获取延迟：平均 ${num(timing.mean_delay_hours,1)} 小时，${timing.samples} 个样本。` : '新闻获取延迟：尚无同时记录公开和实际获取时刻的样本。'),
        text('p',timing.import_delay?.samples ? `工作台导入延迟：平均 ${num(timing.import_delay.mean_delay_hours,1)} 小时，${timing.import_delay.samples} 个样本。` : '工作台导入延迟：尚无可计算小时均值的样本。','subtle'),
        ...(timing.import_delay?.date_only_samples ? [text('p',`${timing.import_delay.date_only_samples} 份资料只确认公开日期，不计算小时级延迟。导入延迟与新闻获取速度分开评价。`,'subtle')] : []),
        text('p',`预测关联：${prediction.samples || 0} 个有明确方向的样本，命中 ${prediction.hits || 0} 个；未校准概率，不自动调权。`,'subtle')
      ]);
      const detail = document.createElement('details'); detail.append(text('summary','展开核验依据与关联样本'));
      arr(facts.checks).forEach(check => detail.append(text('p',`${({correct:'正确',incorrect:'错误',unverified:'待核验'})[check.verdict]}：${check.claim}；依据 ${arr(check.evidence_ids).join('、') || '尚缺'}；${check.note || ''}`)));
      arr(prediction.observations).forEach(item => detail.append(text('p',`${item.symbol} · ${item.review_date}：价格变化 ${num(item.return_pct)}%，研究 ${item.run_id}，行情依据 ${arr(item.source_ids).join('、')}`)));
      if (!arr(facts.checks).length && !arr(prediction.observations).length) detail.append(text('p','尚无已核验记录。'));
      box.append(detail,button('记录事实核验','btn secondary',()=>sourceCheck(source.id))); wrap.append(box);
    });
    return wrap;
  }

  function textForm() {
    const form = document.createElement('form'); form.className = 'card form-card'; form.append(text('h3', '导入文本')); const fields = document.createElement('div'); fields.className = 'form-grid'; fields.append(inputField('标题', 'title', 'text', { required: 'true' }), sourceField(), inputField('来源 URL（可选）', 'url'), inputField('公开时间（可选）', 'published_at', 'datetime-local'), inputField('正文', 'text', 'textarea', { required: 'true', rows: '7' })); fields.lastChild.classList.add('full'); form.append(fields, formActions('保存文本')); form.addEventListener('submit', event => { event.preventDefault(); action(async () => { const data = new FormData(form); await api('/api/materials/text', { method: 'POST', body: JSON.stringify({ request_id: requestId(form), title: data.get('title'), source_id: data.get('source_id'), url: data.get('url') || undefined, published_at: beijingIso(data.get('published_at')), text: data.get('text') }) }); await loadState(); }); }); return form;
  }

  function fileForm() {
    const form = document.createElement('form'); form.className = 'card form-card'; form.append(text('h3', '导入 PDF / 文本文件')); const fields = document.createElement('div'); fields.className = 'form-grid'; fields.append(inputField('标题', 'title', 'text', { required: 'true' }), sourceField(), inputField('来源 URL（可选）', 'url'), inputField('公开时间（可选）', 'published_at', 'datetime-local')); form.append(fields); const drop = document.createElement('div'); drop.className = 'file-drop'; const label = document.createElement('label'); label.textContent = '选择文件（最大 20MB）'; const file = document.createElement('input'); file.type = 'file'; file.name = 'file'; file.accept = '.pdf,.txt,.md'; label.append(file); drop.append(label, text('p', '支持 PDF、TXT、Markdown；扫描件或无法提取时显示服务端状态。')); form.append(drop, formActions('上传资料'));
    form.addEventListener('submit', event => { event.preventDefault(); action(async () => { if (!file.files[0]) throw new Error('请选择文件后再上传'); if (file.files[0].size > 20 * 1024 * 1024) throw new Error('文件超过 20MB 限额'); const data = new FormData(form); data.append('request_id', requestId(form)); if (data.get('published_at')) data.set('published_at', beijingIso(data.get('published_at'))); await api('/api/materials/file', { method: 'POST', body: data }); await loadState(); }); }); return form;
  }

  function sourceField() {
    return selectField('原文来源标注（不自动连接）', 'source_id', [['manual','用户提供的资料'], ...knownSourceOptions().filter(([id]) => id !== 'manual')]);
  }

  function evidenceRow(item) {
    const row = document.createElement('article'); row.className='source-row';
    const info = document.createElement('div');
    const status = ({ready:'可读',needs_text:'需补文字',error:'获取失败'})[item.status] || item.status;
    info.append(text('div',item.title || item.id,'run-title'),text('div',`${status} · ${item.source_id} · 获取 ${fmtTime(item.captured_at)}`,'evidence-meta'));
    arr(item.warnings).forEach(warning => info.append(text('p',warning,'source-eval')));
    row.append(info,button('查看原文','btn secondary',()=>action(async()=>showModal(item.title || item.id,evidenceDetail(await api(`/api/evidence/${encodeURIComponent(item.id)}`))))));
    return row;
  }

  function evidenceDetail(item, annotations = []) {
    const wrap = document.createElement('div');
    const published = item.metadata?.published_at_precision === 'date' ? `${fmtDate(item.published_at)}（仅确认日期）` : fmtTime(item.published_at);
    wrap.append(text('p',`证据编号：${item.id}`,'evidence-meta'),text('p',`公开：${published}；获取：${fmtTime(item.captured_at)}；可知：${fmtTime(item.available_at)}`,'evidence-meta'));
    annotations.filter(note => !arr(item.metadata?.warnings).includes(note)).forEach(note => wrap.append(text('p', `后续补充说明（冻结原件不改动）：${note}`, 'source-eval')));
    if (item.url && /^https?:\/\//.test(item.url)) {
      const link = text('a','打开来源页面'); link.href=item.url; link.target='_blank'; link.rel='noopener noreferrer'; wrap.append(link);
    }
    if (item.metadata?.original_path) wrap.append(text('p',`本地原件：${item.metadata.original_path}`,'evidence-meta'));
    if (item.source_id === 'caixin' && item.metadata?.adapter_version) wrap.append(caixinEvidenceDetails(item));
    arr(item.metadata?.warnings).forEach(warning=>wrap.append(text('p',warning,'source-eval')));
    if (item.status !== 'ready') wrap.append(text('p','当前提取未达到可读标准，不作为已读事实。','source-eval'));
    wrap.append(text('div',item.text || '没有可靠提取的正文。','evidence-text'));
    const paragraphs = arr(item.metadata?.paragraphs);
    if (item.source_id === 'caixin' && item.metadata?.adapter_version && paragraphs.length) {
      const paragraphDetail = document.createElement('details');
      paragraphDetail.append(text('summary', `段落定位（${paragraphs.length} 段）`));
      const codepoints = Array.from(item.text || '');
      paragraphs.forEach((paragraph, index) => {
        const start = Number(paragraph.start);
        const end = Number(paragraph.end);
        const located = Number.isInteger(start) && Number.isInteger(end) && typeof item.text === 'string' ? codepoints.slice(start, end).join('') : '';
        const line = paragraph.start_line != null ? ` · 第 ${paragraph.start_line}-${paragraph.end_line ?? paragraph.start_line} 行` : '';
        paragraphDetail.append(text('h4', `第 ${index + 1} 段 · 字符 ${Number.isInteger(start) ? start : '待确认'}-${Number.isInteger(end) ? end : '待确认'}${line}`));
        paragraphDetail.append(text('div', located || '该段位置尚未能从正文精确提取。', 'evidence-text'));
      });
      wrap.append(paragraphDetail);
    }
    const pages=arr(item.metadata?.pages);
    if (pages.length) {
      const detail=document.createElement('details'); detail.append(text('summary',`逐页提取（${pages.length} 页）`),text('div',pages.map(page=>`第 ${page.page} 页 · ${page.status === 'ready' ? '可读' : '待处理'}\n${page.text}`).join('\n\n'),'evidence-text')); wrap.append(detail);
    }
    return wrap;
  }

  function caixinEvidenceDetails(item) {
    const metadata = item.metadata || {};
    const details = document.createElement('details');
    details.append(text('summary', '财新来源定位与完整性'));
    const root = state.caixinStatus?.root;
    const rawRoot = state.caixinStatus?.raw_root;
    if (metadata.distilled_relative_path) details.append(text('p', `清洗正文相对路径：${metadata.distilled_relative_path}`, 'evidence-meta'));
    if (root && metadata.distilled_relative_path) details.append(text('p', `清洗正文完整路径：${joinLocalPath(root, metadata.distilled_relative_path)}`, 'evidence-meta'));
    const rawPaths = arr(metadata.raw_paths);
    const claimed = arr(metadata.raw_sha256);
    const actual = arr(metadata.raw_actual_sha256);
    if (rawPaths.length) {
      details.append(text('p', '原件路径与哈希校验：', 'evidence-meta'));
      rawPaths.forEach((path, index) => {
        const expected = claimed[index];
        const observed = actual[index];
        const check = expected && observed ? (expected === observed ? '校验通过' : '校验失败') : '待核验';
        details.append(text('p', `${joinLocalPath(rawRoot, path)} · ${check}${expected ? ` · SHA-256 ${expected}` : ''}`, check === '校验失败' ? 'source-eval' : 'evidence-meta'));
      });
    } else details.append(text('p', '没有可定位的原件路径。', 'source-eval'));
    return details;
  }

  function joinLocalPath(root, relative) {
    if (!relative) return '待确认';
    if (!root || /^[A-Za-z]:[\\/]/.test(relative) || relative.startsWith('\\\\')) return relative;
    return `${String(root).replace(/[\\/]$/, '')}/${String(relative).replace(/^[\\/]+/, '')}`;
  }

  function sourceCheck(sourceId) {
    const form = document.createElement('form'); form.className = 'form-card'; form.append(text('h3', `评价来源：${sourceId || '未标识'}`)); const fields = document.createElement('div'); fields.className = 'form-grid'; fields.append(inputField('待评价主张', 'claim', 'textarea', { required: 'true', rows: '4' }), inputField('备注', 'note', 'textarea', { rows: '4' })); const verdict = selectField('评价', 'verdict', [['correct', '已核实正确'], ['incorrect', '已核实错误'], ['unverified', '待评']]); const evidence = selectField('核验依据（可多选）', 'evidence_ids', []); evidence.querySelector('select').multiple = true; arr(state.data?.evidence).filter(item => item.status === 'ready').forEach(item => { const option = text('option', `${item.title || item.id} · ${item.id}`); option.value = item.id; evidence.querySelector('select').append(option); }); fields.append(verdict, evidence); form.append(fields, formActions('保存评价')); form.addEventListener('submit', event => { event.preventDefault(); action(async () => { const data = new FormData(form); const selected = [...evidence.querySelector('select').selectedOptions].map(option => option.value); await api('/api/source-checks', { method: 'POST', body: JSON.stringify({ request_id: requestId(form), source_id: sourceId, claim: data.get('claim'), verdict: data.get('verdict'), evidence_ids: selected, note: data.get('note') }) }); document.querySelector('.modal-backdrop')?.remove(); await loadState(); }); }); showModal('来源评价', form);
  }

  function renderLedger() {
    const view=$('#view-ledger'); view.replaceChildren(); const summary=obj(state.data?.ledger);
    view.append(sectionHead('记录与复盘','实际账户收益与研究判断分开评价；模拟和未执行记录不改变实际资金。'));
    const accountPanel = reportedAccountPanel(); if (accountPanel) view.append(accountPanel);
    view.append(card([
      metric('账户净值',money(summary.net_value),summary.cash_estimated ? '费用未知，当前为估计' : '现金 + 持仓市值'),
      metric('累计净收益',money(summary.net_profit),'净值 − 起点净值 − 净入金'),
      metric('相对被动对照',money(summary.incremental_profit),'与初始持仓保持不动相比'),
      metric('完整月均',money(summary.monthly_average),`${arr(summary.complete_months).length} 个有完整边界的月份`),
      metric('现金',money(summary.cash),'人工账本余额'),
      metric('期间净入金',money(summary.net_inflows),'新增本金不计收益'),
      metric('已实现盈亏',money(summary.realized_pnl),'持仓成本口径，含已知费用'),
      metric('浮动盈亏',money(summary.unrealized_pnl),'当前价格与持仓成本之差'),
      metric('收到分红',money(summary.dividends),'按实际到账金额'),
      metric('实际费用',summary.fees_complete ? money(summary.fees) : '待确认','费用未齐时不显示净盈利'),
      metric('被动对照净值',money(summary.benchmark_value),'相同资金进出与初始持仓分红'),
      metric('最大回撤金额',money(summary.max_drawdown_amount),summary.drawdown_scope || '只覆盖已记录估值点')
    ],'grid grid-4'));
    if (summary.confirmed) view.append(text('p','已实现与浮动盈亏使用历史持仓成本；累计净收益从确认起点计量，三者不一定直接相加。','subtle'));
    arr(summary.warnings).forEach(warning=>view.append(text('p',warning,'notice')));
    if (!summary.confirmed) view.append(empty('账户尚未确认','请确认现金、当前持仓、成本与测量起点；不预填历史聊天中的账户金额。'));
    else {
      view.append(sectionHead('当前持仓',`确认起点：${fmtTime(summary.baseline?.as_of)}；起点净值 ${money(summary.initial_value)}`));
      view.append(arr(summary.positions).length ? dataTable(['标的','数量','平均成本','估值价格','持仓市值','浮动盈亏'],summary.positions.map(p=>[p.symbol,p.quantity,num(p.avg_cost,4),num(p.price,3),money(p.market_value),money(p.unrealized_pnl)])) : empty('当前为全现金账户','持仓为空，不代表账户收益已得到长期验证。'));
    }
    const forms=document.createElement('div'); forms.className='grid grid-2';
    if (!summary.confirmed) forms.append(baselineForm());
    forms.append(eventForm(), planEventForm()); if (summary.confirmed) forms.append(valuationForm(summary));
    view.append(sectionHead('人工记录','实际成交由你执行后再录入；这里不连接券商。'),forms);
    view.append(sectionHead('事件历史','补录费用时引用原交易编号；已保存的记录不会被覆盖。'),eventHistory(arr(summary.events)));
    view.append(sectionHead('估值历史','同一时点应填写全部实际持仓和初始被动对照标的的价格。'),valuationHistory(arr(summary.valuations)));
    if (arr(summary.complete_months).length) view.append(sectionHead('完整月份'),dataTable(['月份','净收益'],summary.complete_months.map(m=>[m.month,money(m.net_profit)])));
    view.append(performancePanel(), paperPanel());
    arr(state.data?.runs).filter(r=>r.decision).forEach(r=>view.append(button(`查看 ${fmtTime(r.as_of)} 的执行与机会成本`, 'btn secondary', ()=>action(async()=>{const v=await api(`/api/runs/${encodeURIComponent(r.id)}/outcomes`);showModal('条件路径与执行结果',outcomesPanel(v.outcomes));}))));
    view.append(sectionHead('计划复核','第1/5/20交易日及事先声明期限；价格变化不是账户收益。'));
    const rows=arr(state.data?.runs).filter(run=>run.mode !== 'replay').flatMap(run=>reviewRows(run).map(row=>[row.symbol,row.review_date,row.observation?.status === 'completed' ? '已核对' : '待核对',row.observation?.return_pct == null ? '—' : num(row.observation.return_pct)+'%',row.observation?.pending_reason || '',run.id]));
    view.append(rows.length ? dataTable(['标的','复核日','状态','价格变化','缺口','研究编号'],rows) : empty('尚无已发布计划','完成一轮研究后，会列出预定复核日期。'));
  }

  function planSelect(required) {
    const options=required?[]:[['','不关联计划']];
    arr(state.data?.runs).forEach(r=>arr(r.decision?.plans).forEach(p=>options.push([`${r.id}|${p.plan_id}|${p.symbol}`,`${fmtTime(r.as_of)} · ${p.symbol} · ${p.action} · ${r.mode==='replay'?'历史回放':'前瞻研究'}`])));
    const field=selectField('关联原始计划','plan_ref',options);field.querySelector('select').required=required;return field;
  }

  function planEventForm() {
    const f=document.createElement('form');f.className='card form-card';
    f.append(text('h3','记录条件与未执行原因'),planSelect(true),selectField('计划事件','kind',[['triggered','已触发'],['not_executed','未执行'],['invalidated','已失效'],['expired','已到期']]),inputField('核对时间','as_of','datetime-local',{required:'true'}),inputField('核对依据','source','text',{required:'true'}),inputField('原因与说明','note','textarea',{required:'true',rows:'3'}),formActions('保存计划记录'));
    f.addEventListener('submit',e=>{e.preventDefault();action(async()=>{const d=new FormData(f),[rid,pid]=d.get('plan_ref').split('|');await api('/api/plans/events',{method:'POST',body:JSON.stringify({request_id:requestId(f),run_id:rid,plan_id:pid,kind:d.get('kind'),as_of:beijingIso(d.get('as_of')),source:d.get('source'),note:d.get('note')})});await loadState();});});return f;
  }

  function paperPanel() {
    const p=obj(state.data?.paper_ledger),d=document.createElement('details');d.className='card';d.append(text('summary','独立模拟账户'));
    d.append(text('p','先声明模拟起点，再用事件表的“模拟”状态记录。模拟净值、费用、资金和持仓与实际账户分别计算。'));
    if(!p.confirmed)d.append(baselineForm('paper'));
    else {d.append(text('p',`模拟净值 ${money(p.net_value)}；净收益 ${money(p.net_profit)}；相对被动持有 ${money(p.incremental_profit)}`),valuationForm(p,'paper'));arr(p.warnings).forEach(w=>d.append(text('p',w,'notice')));}
    return d;
  }

  function performancePanel() {
    const p=obj(state.data?.performance),s=document.createElement('section');s.className='card';
    s.append(text('h3',`阶段目标：长期平均每月净收益 ${money(p.monthly_target)}`),text('p',p.goal_status||'尚未开始计量'),text('p',`实际账户完整月均 ${money(p.monthly_average)}；期间账户净收益 ${money(p.account_net_profit)}；同期间扣运行成本 ${money(p.profit_after_operating_costs)}`),text('p','目前尚未证明稳定盈利。方向正确、条件回放和实际账户利润分别评价；不因目标倒推交易次数或提高风险。','subtle'));
    if(!p.operating_costs_complete)s.append(text('p','模型、数据或订阅账单期间未覆盖完整，暂不宣称扣除项目成本后的盈利。','notice'));
    s.append(text('p',`已覆盖完整月份的扣运行成本后月均：${money(p.monthly_average_after_operating_costs)}`));
    const history=document.createElement('form');history.append(inputField('核对历史截止日','through','date',{required:'true'}),formActions('查看该日收益与成本'));
    history.addEventListener('submit',e=>{e.preventDefault();action(async()=>{const at=history.elements.through.value+'T23:59:59+08:00',v=await api('/api/performance?as_of='+encodeURIComponent(at));const body=card([text('p',`截止 ${fmtTime(v.as_of)}，账户净收益 ${money(v.account_net_profit)}；扣同期间运行成本 ${money(v.profit_after_operating_costs)}；相对被动对照并扣运行成本 ${money(v.incremental_profit_after_operating_costs)}`),text('p',v.operating_costs_complete?'成本期间完整':'账单未完整覆盖该期间，不能给出扣成本结果')]);showModal('历史收益核对',body);});});s.append(history);
    arr(p.strategies).forEach(g=>s.append(text('p',`${g.strategy_key}：${g.plan_count}条事前计划，${arr(g.time_clusters).length}个不重叠时间簇；${g.interpretation}`)));
    const d=document.createElement('details'),f=document.createElement('form');d.append(text('summary','登记已结束期间的运行成本'));
    f.append(inputField('起始日期','start_date','date',{required:'true'}),inputField('结束日期（最晚昨天）','end_date','date',{required:'true'}),inputField('该期间全部成本合计（元）','amount','number',{min:'0',step:'0.01',required:'true'}),inputField('账单与分摊依据','source','text',{required:'true'}));
    const label=document.createElement('label'),check=document.createElement('input');check.type='checkbox';check.required=true;label.append(check,text('span','确认包含该期间全部模型、数据和订阅成本；真实零成本才填0'));f.append(label,formActions('确认期间成本'));
    f.addEventListener('submit',e=>{e.preventDefault();action(async()=>{const d=new FormData(f);await api('/api/project-costs',{method:'POST',body:JSON.stringify({request_id:requestId(f),start_date:d.get('start_date'),end_date:d.get('end_date'),amount:Number(d.get('amount')),source:d.get('source'),confirmed_complete:check.checked})});await loadState();});});d.append(f);s.append(d);return s;
  }

  function dataTable(headers, rows) {
    const wrap=document.createElement('div'); wrap.className='table-wrap'; const table=document.createElement('table');
    const head=document.createElement('tr'); headers.forEach(h=>head.append(text('th',h))); const thead=document.createElement('thead'); thead.append(head); table.append(thead);
    const tbody=document.createElement('tbody'); rows.forEach(row=>{const tr=document.createElement('tr'); row.forEach(value=>tr.append(text('td',value))); tbody.append(tr);});
    table.append(tbody); wrap.append(table); return wrap;
  }

  function baselineForm(mode = 'actual') {
    const form = document.createElement('form'); form.className = 'card form-card'; form.append(text('h3', mode === 'paper' ? '建立独立模拟起点' : '确认初始基线')); const fields = document.createElement('div'); fields.className = 'form-grid'; fields.append(inputField('确认日期时间', 'as_of', 'datetime-local', { required: 'true' }), inputField('现金', 'cash', 'number', { step: '0.01', min: '0', required: 'true' })); const check = document.createElement('label'); check.className = 'checkline field full'; const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.required = true; checkbox.name = 'confirmed'; check.append(checkbox, text('span', mode === 'paper' ? '以上只用于模拟，不作为真实账户' : '我确认以上为当前真实账户起点')); fields.append(check); const positions = document.createElement('div'); positions.className = 'field full'; positions.append(text('label', '当前持仓（可留空，全现金账户也可提交）')); const rows = document.createElement('div'); positions.append(rows, button('增加标的', 'btn secondary', () => addPositionRow(rows))); fields.append(positions); form.append(fields, formActions('保存基线')); form.addEventListener('submit', event => { event.preventDefault(); action(async () => { const data = new FormData(form); const positionsData = $$('[data-position-row]', rows).map(row => ({ symbol: $('[name=symbol]', row).value.trim(), quantity: Number($('[name=quantity]', row).value), avg_cost: Number($('[name=avg_cost]', row).value), price: Number($('[name=price]', row).value) })).filter(item => item.symbol); await api(mode === 'paper' ? '/api/ledger/paper/baseline' : '/api/ledger/baseline', { method: 'POST', body: JSON.stringify({ request_id: requestId(form), confirmed: true, as_of: beijingIso(data.get('as_of')), cash: Number(data.get('cash')), positions: positionsData }) }); await loadState(); }); }); return form;
  }

  function addPositionRow(root) { const row = document.createElement('div'); row.dataset.positionRow = 'true'; row.className = 'position-table'; row.append(input('symbol', '代码', '', true), input('quantity', '数量', '', true, 'number', '1'), input('avg_cost', '成本', '', true, 'number', 'any'), input('price', '价格', '', true, 'number', '0.001'), button('删除', 'btn danger', () => row.remove())); root.append(row); }

  function eventForm() {
    const form = document.createElement('form'); form.className = 'card form-card'; form.append(text('h3', '记录事件')); const fields = document.createElement('div'); fields.className = 'form-grid'; fields.append(planSelect(false)); const typeField = selectField('事件类型', 'type', [['buy', '买入'], ['sell', '卖出'], ['dividend', '分红'], ['deposit', '入金'], ['withdrawal', '出金'], ['fee', '费用']]); const dividendField = inputField('每股税后分红（仅分红）', 'dividend_per_share', 'number', { min: '0', step: '0.0001' }); dividendField.hidden = true; typeField.querySelector('select').addEventListener('change', event => { dividendField.hidden = event.target.value !== 'dividend'; }); fields.append(typeField, selectField('状态', 'status', [['actual', '实际'], ['paper', '模拟'], ['not_executed', '未执行']]), inputField('发生时间', 'as_of', 'datetime-local', { required: 'true' }), inputField('标的（可选）', 'symbol'), inputField('数量（可选）', 'quantity', 'number', { min: '1', step: '1' }), inputField('价格（可选）', 'price', 'number', { min: '0', step: '0.001' }), inputField('金额（可选）', 'amount', 'number', { min: '0', step: '0.01' }), inputField('费用（未知可留空）', 'fees', 'number', { min: '0', step: '0.01' }), dividendField, inputField('关联事件 ID（可选）', 'related_event_id'), inputField('备注', 'note', 'textarea', { rows: '2' })); form.append(fields, formActions('保存事件')); form.addEventListener('submit', event => { event.preventDefault(); action(async () => { const data = new FormData(form); const type = data.get('type'); const payload = { request_id: requestId(form), as_of: beijingIso(data.get('as_of')), type, status: data.get('status'), symbol: data.get('symbol') || undefined, quantity: data.get('quantity') ? Number(data.get('quantity')) : undefined, price: data.get('price') ? Number(data.get('price')) : undefined, amount: data.get('amount') ? Number(data.get('amount')) : undefined, fees: data.get('fees') ? Number(data.get('fees')) : null, related_event_id: data.get('related_event_id') || undefined, note: data.get('note') || undefined }; if(data.get('plan_ref')) {const [rid,pid,symbol]=data.get('plan_ref').split('|');payload.run_id=rid;payload.plan_id=pid;if(!payload.symbol)payload.symbol=symbol;} if (type === 'dividend' && data.get('dividend_per_share')) payload.dividend_per_share = Number(data.get('dividend_per_share')); await api('/api/ledger/events', { method: 'POST', body: JSON.stringify(payload) }); await loadState(); }); }); return form;
  }

  function valuationForm(summary, mode = 'actual') {
    const form = document.createElement('form'); form.className = 'card form-card'; form.append(text('h3', mode === 'paper' ? '保存模拟估值' : '保存实际估值点')); const fields = document.createElement('div'); fields.className = 'form-grid'; fields.append(inputField('估值日期时间', 'as_of', 'datetime-local', { required: 'true' })); const prices = document.createElement('div'); prices.className = 'field full'; prices.append(text('label', '持仓价格（支持多个标的）')); const rows = document.createElement('div'); const symbols = [...new Set([...arr(summary?.positions), ...arr(summary?.baseline?.positions)].map(item => item.symbol).filter(Boolean))]; symbols.forEach(symbol => addPriceRow(rows, symbol)); prices.append(rows, button('增加标的', 'btn secondary', () => addPriceRow(rows)), button('填入已获取报价', 'btn secondary', () => fillQuotes(rows))); fields.append(prices); form.append(fields, formActions('提交估值')); form.addEventListener('submit', event => { event.preventDefault(); action(async () => { const data = new FormData(form); const priceMap = {}; $$('[data-price-row]', rows).forEach(row => { const symbol = $('[name=symbol]', row).value.trim(); if (symbol) priceMap[symbol] = Number($('[name=price]', row).value); }); await api(mode === 'paper' ? '/api/ledger/paper/valuations' : '/api/ledger/valuations', { method: 'POST', body: JSON.stringify({ request_id: requestId(form), as_of: beijingIso(data.get('as_of')), prices: priceMap }) }); await loadState(); }); }); return form;
  }

  function addPriceRow(root, symbol = '') { const row = document.createElement('div'); row.dataset.priceRow = 'true'; row.className = 'position-table'; row.append(input('symbol', '代码', symbol, true), input('price', '价格', '', true, 'number', '0.001'), button('删除', 'btn danger', () => row.remove())); root.append(row); }
  function fillQuotes(root) { const quotes = arr(state.data?.market?.latest?.metadata?.quotes); $$('[data-price-row]', root).forEach(row => { const quote = quotes.find(item => item.symbol === $('[name=symbol]', row).value.trim()); if (quote && !quote.stale && quote.price !== null && quote.price !== undefined) $('[name=price]', row).value = quote.price; }); notice('已填入未过时的报价；空白项需补充。请按报价时点填写估值时间。'); }
  function eventHistory(events) {
    if (!events.length) return empty('暂无事件记录','实际、模拟和未执行事件将在保存后显示。');
    const list=document.createElement('div'); list.className='source-list';
    events.slice().reverse().forEach(event=>{
      const row=document.createElement('div'); row.className='source-row'; const info=document.createElement('div');
      const correction=events.find(item=>item.type === 'fee' && item.related_event_id === event.id && item.status === event.status);
      const fee=['buy','sell'].includes(event.type) ? `费用 ${event.fees == null && correction ? money(correction.amount)+'（已补录）' : money(event.fees)}` : '';
      info.append(text('div',`${({buy:'买入',sell:'卖出',dividend:'分红',deposit:'入金',withdrawal:'出金',fee:'费用'})[event.type]} · ${({actual:'实际',paper:'模拟',not_executed:'未执行'})[event.status]} · ${event.symbol || '现金'}`,'run-title'),text('p',`${fmtTime(event.as_of)}　数量 ${event.quantity ?? '—'}　金额 ${money(event.amount)}　${fee}`,'run-meta'),text('p',`编号：${event.id}${event.related_event_id ? '；关联 '+event.related_event_id : ''}`,'evidence-meta'));
      row.append(info,button('复制编号','btn secondary',()=>{if(navigator.clipboard) navigator.clipboard.writeText(event.id).then(()=>notice('编号已复制。'));else showModal('事件编号',text('p',event.id));})); list.append(row);
    }); return list;
  }

  function valuationHistory(valuations) { if (!valuations.length) return empty('暂无估值历史', '提交真实估值点后，这里会显示覆盖范围。'); const list = document.createElement('div'); list.className = 'source-list'; valuations.slice().reverse().forEach(item => list.append(card([text('div', fmtTime(item.as_of), 'run-title'), text('div', JSON.stringify(item.prices || item), 'run-meta')], 'source-row'))); return list; }

  function input(name, placeholder, value = '', required = false, type = 'text', step) { const element = document.createElement('input'); element.name = name; element.setAttribute('aria-label',placeholder); element.placeholder = placeholder; element.value = value; element.type = type; element.required = required; if (step) element.step = step; return element; }
  function inputField(label, name, type = 'text', attrs = {}) { const wrap = document.createElement('div'); wrap.className = 'field'; wrap.append(text('label', label)); const element = type === 'textarea' ? document.createElement('textarea') : document.createElement('input'); element.name = name; element.setAttribute('aria-label',label); if (type !== 'textarea') element.type = type; Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, value)); wrap.append(element); return wrap; }
  function selectField(label, name, options) { const wrap = document.createElement('div'); wrap.className = 'field'; wrap.append(text('label', label)); const select = document.createElement('select'); select.name = name; select.setAttribute('aria-label',label); options.forEach(([value, labelText]) => { const option = text('option', labelText); option.value = value; select.append(option); }); wrap.append(select); return wrap; }
  function formActions(label) { const actions = document.createElement('div'); actions.className = 'form-actions'; actions.append(button(label, 'btn primary', null, { type: 'submit' })); return actions; }
  function showModal(title, body) { const backdrop = document.createElement('div'); backdrop.className = 'modal-backdrop'; const modal = document.createElement('div'); modal.className = 'modal'; const head = document.createElement('div'); head.className = 'modal-head'; head.append(text('h2', title), button('×', 'modal-close', () => backdrop.remove(), { 'aria-label': '关闭' })); modal.append(head, body); backdrop.append(modal); backdrop.addEventListener('click', event => { if (event.target === backdrop) backdrop.remove(); }); document.body.append(backdrop); }

  $$('.nav-tab').forEach(tab => tab.addEventListener('click', () => setTab(tab.dataset.tab)));
  $('#reload-state').addEventListener('click', () => withBusy(loadState));
  loadState();
})();
