"""Business acceptance: frozen plan -> path -> recorded trade -> role's next context."""
import copy
import pytest
from fastapi.testclient import TestClient
from app.store import Store, DomainError, ConflictError
from app.workflow import Workflow
from app.ledger import Ledger
from app.outcomes import PlanBook, evaluate_plan, normalize_execution, scenario_metrics
from app.market import market_readiness
from app.packets import validate_packet, import_packet, latest_packet, CATEGORIES
from app.performance import record_costs, summary
from app.settings import update_settings
from app.server import create_app
from test_workflow import put_market, opinion, decision

START = '2026-09-21T16:30:00+08:00'
END = '2026-09-24T16:30:00+08:00'


@pytest.fixture
def timebox(monkeypatch):
    current = [START]
    for name in ('workflow', 'ledger', 'outcomes', 'market', 'packets', 'performance', 'runner'):
        monkeypatch.setattr(f'app.{name}.now_iso', lambda: current[0])
    return current


def published(store, action='buy', kind='limit', horizon=3, supersedes=None, request='run'):
    if not store.read('evidence/market-1.json'):
        put_market(store, 'market-1', available_at='2026-09-21T15:30:00+08:00',
                   quote_as_of='2026-09-21T15:00:00+08:00',
                   rows=[bar('2026-09-21', 10, 10.2, 9.8, 10)])
    w=Workflow(store)
    r=w.prepare(dict(request_id=request, mode='live', as_of=START, symbols=['601398'],
                     evidence_ids=['market-1'], decision_contract=3))
    for phase in ('initial','challenge'):
        for role in ('opportunity','counter'):
            body=opinion(role+' '+phase,['market-1'])
            for f in body['forecasts']:
                f['expected_direction']='up' if role=='opportunity' else 'down'
            w.submit_role(r['id'],role,phase,body)
    d=decision(horizon,supersedes=supersedes)
    d['argument_resolution']={'opportunity':'保留上行机制，价格纪律优先','counter':'接受下行风险，固定退出规则'}
    p=d['plans'][0]
    p.update(action=action,evidence_ids=['market-1'],decision_basis='比较持有、现金与条件入场',
             long_term_view='假说有待前瞻验证',sizing={'basis':'测试价格区间','assumptions':['费用明确仅为假设'],
             'scenarios':[{'label':'100股情景','quantity':100,'entry_price':10,'exit_price':11,
                           'stop_price':9,'estimated_fees':2,'reasoning':'冻结情景'}]},
             edge={'mechanism':'测试因果假说','alternative_explanation':'随机波动','priced_in':'无法确认已计价程度',
                   'falsifier':'经营事实逆转','probability_basis':'unknown','win_probability':None,
                   'sample_refs':[],'independent_samples':None},
             execution={'strategy_key':'rebound-hypothesis','assumptions':['测试数据非真实收益'],
                        'legs':[{'action':action,'quantity':100,'entry':{'kind':kind,'price':10 if kind in {'limit','stop','close_above','close_below'} else None},
                                 'take_profit':11 if action in {'buy','hold'} else None,
                                 'stop_loss':9 if action in {'buy','hold'} else None,'exit_basis':'intraday',
                                 'conditions':[],'buy_fee':1,'sell_fee':1,'slippage_bps':0,'reasoning':'事前规则'}]})
    out=w.publish(r['id'],d)
    return w,r,out,out['plans'][0]


def bar(day, o, h, l, c, volume=1000):
    return dict(date=day,open=o,high=h,low=l,close=c,volume=volume)


@pytest.mark.parametrize('field,value', [('buy_fee', 1), ('sell_fee', 1), ('slippage_bps', 1), ('buy_fee', None)])
def test_zero_friction_cannot_reintroduce_execution_costs(timebox, tmp_path, field, value):
    w, run, decision, _ = published(Store(tmp_path))
    base = w.get_run(run['id'])
    base['research_brief']['cost_model'] = {'mode': 'zero_friction'}
    draft = copy.deepcopy(decision)
    plan = draft['plans'][0]
    plan['sizing']['scenarios'][0]['estimated_fees'] = 0
    plan['execution']['legs'][0].update(buy_fee=0, sell_fee=0, slippage_bps=0)
    plan['execution']['legs'][0][field] = value
    with pytest.raises(DomainError, match='零摩擦'):
        w._normalise_decision(draft, base)
    plan['execution']['legs'][0][field] = 0
    normalized = w._normalise_decision(draft, base)['plans'][0]
    assert normalized['research_cost_mode'] == 'zero_friction'
    evidence = future(w.store, losing_path())
    outcome = evaluate_plan(normalized, decision, [evidence], END)
    assert outcome['estimated_net_pnl'] == outcome['gross_pnl']
    assert outcome['research_cost_mode'] == 'zero_friction'


def future(store, rows):
    return put_market(store,'future',available_at=END,quote_as_of='2026-09-24T15:00:00+08:00',
                       quote_price=rows[-1]['close'],rows=rows)


def losing_path():
    return [bar('2026-09-22',10,10.8,8.8,9.5),bar('2026-09-23',8.8,9.2,8.5,9.1),
            bar('2026-09-24',10,11.5,9.9,11.2)]


def test_direction_right_but_plan_loses_and_own_roles_learn(timebox,tmp_path):
    store=Store(tmp_path);w,r,d,p=published(store)
    original=copy.deepcopy(d)
    timebox[0]=END;e=future(store,losing_path())
    review=w.review(r['id'],dict(request_id='review',as_of=END,evidence_ids=[e['id']],
                               reflections={k:'保留证据，分清方向和执行' for k in ('opportunity','counter','judge')}))
    outcome=review['plan_outcomes'][0]
    assert outcome['gross_pnl']==-120
    assert outcome['estimated_net_pnl']==-122
    assert outcome['legs'][0]['fills'][1]['date']=='2026-09-23'
    assert 'T+1' in outcome['legs'][0]['issues'][0]
    assert any(x['actual_direction']=='up' for x in review['observations'] if x['status']=='completed')
    assert store.read(f"run_events/{r['id']}/decision.json")==original
    op=review['role_assessments']['opportunity']['own_opinions']['initial']['forecasts'][0]
    co=review['role_assessments']['counter']['own_opinions']['initial']['forecasts'][0]
    assert op['direction_hit'] is False and co['direction_hit'] is True
    next_run=w.prepare(dict(request_id='next',mode='live',as_of=END,symbols=['601398'],evidence_ids=['future']))
    ctx=w.context(next_run['id'],'opportunity','initial')
    assert ctx['memory'][0]['plan_outcomes'][0]['estimated_net_pnl']==-122
    assert ctx['memory'][0]['own_assessment']['own_opinions']['initial']['forecasts'][0]['direction_hit'] is False


@pytest.mark.parametrize('change,expected',[
    ('ambiguous','unresolved'),('missing','pending'),('locked','unresolved'),('unknown_fee','expired'),
    ('manual','unresolved'),('untriggered','expired'),('future_available','pending'),('discontinuity','corporate_action_unresolved')])
def test_no_invented_profits_from_incomplete_paths(timebox,tmp_path,change,expected):
    store=Store(tmp_path);w,r,d,p=published(store)
    rows=losing_path()
    if change=='ambiguous': rows[1]=bar('2026-09-23',10,11.2,8.8,10.5)
    if change=='missing': rows.pop(1)
    if change=='locked': rows[0]=bar('2026-09-22',10,10,10,10)
    if change=='unknown_fee': p['execution']['legs'][0]['buy_fee']=None
    if change=='manual': p['execution']['legs'][0]['conditions']=['核实公告']
    if change=='untriggered': p['execution']['legs'][0]['entry']['price']=7
    if change=='discontinuity': rows[0]=bar('2026-09-22',5,5.1,4.9,5)
    e=future(store,rows)
    if change=='future_available': e['available_at']='2026-09-28T16:00:00+08:00'
    value=evaluate_plan(p,d,[e],END)
    assert value['status']==expected
    if change=='unknown_fee': assert value['estimated_net_pnl'] is None
    elif change=='untriggered': assert value['gross_pnl']==0 and value['legs'][0]['status']=='expired_untriggered'
    else: assert value['gross_pnl'] is None


def test_close_signal_does_not_fill_same_day_or_after_expiry(timebox,tmp_path):
    store=Store(tmp_path);w,r,d,p=published(store,kind='close_above')
    e=future(store,[bar('2026-09-22',9.5,9.9,9.1,9.6),bar('2026-09-23',9.7,9.9,9.4,9.8),bar('2026-09-24',9.8,10.3,9.7,10.2)])
    value=evaluate_plan(p,d,[e],END)['legs'][0]
    assert value['status']=='expired_pending_unfilled' and value['fills']==[] and value['gross_pnl']==0


def test_sell_opportunity_cost_and_unknown_probability(timebox,tmp_path):
    store=Store(tmp_path);w,r,d,p=published(store,action='sell',kind='next_open')
    e=future(store,[bar('2026-09-22',10,10.4,9.8,10.2),bar('2026-09-23',10.2,10.8,10,10.7),bar('2026-09-24',10.7,11.2,10.5,11)])
    value=evaluate_plan(p,d,[e],END)
    assert value['gross_pnl']==0 and value['incremental_estimated_pnl']==-101
    metrics=scenario_metrics(p)[0]
    assert metrics['estimated_ev'] is None and metrics['breakeven_probability_with_cost']==.51
    p['execution']['legs'][0]['slippage_bps']=10000
    with pytest.raises(DomainError,match='slippage'): normalize_execution(p['execution'],p)


def test_trade_link_paper_isolation_fee_completion_and_replay_boundary(timebox,tmp_path):
    store=Store(tmp_path);w,r,d,p=published(store)
    actual=Ledger(store);paper=Ledger(store,'paper')
    for book in (actual,paper):book.set_baseline(dict(request_id=book.mode,confirmed=True,as_of=START,cash=7000,positions=[]))
    timebox[0]='2026-09-22T10:00:00+08:00'
    buy=dict(request_id='paper-buy',as_of=timebox[0],type='buy',status='paper',symbol='601398',quantity=100,price=10,fees=None,run_id=r['id'],plan_id=p['plan_id'])
    event=actual.record_event(buy)
    assert actual.record_event(buy)==event
    assert actual.summary()['cash']==7000 and paper.summary({'601398':10})['net_profit'] is None
    with pytest.raises(DomainError,match='可卖'):actual.record_event({**buy,'request_id':'same-day-sell','type':'sell','fees':1})
    timebox[0]='2026-09-23T10:00:00+08:00'
    actual.record_event({**buy,'request_id':'paper-sell','as_of':timebox[0],'type':'sell','price':11,'fees':1})
    pb=PlanBook(store)
    assert pb.attribution(r['id'],p['plan_id'],11)['paper']['incremental_trade_pnl'] is None
    actual.record_event(dict(request_id='fee-fix',as_of=timebox[0],type='fee',status='paper',amount=1,related_event_id=event['id']))
    assert pb.attribution(r['id'],p['plan_id'],11)['paper']['closed_roundtrip_net']==98
    assert paper.summary({'601398':11})['net_profit']==98 and actual.summary()['net_profit']==0
    with pytest.raises(DomainError,match='早于'):actual.record_event({**buy,'request_id':'backfill','as_of':'2026-09-21T10:00:00+08:00','status':'not_executed'})
    with pytest.raises(DomainError,match='不属于'):actual.record_event({**buy,'request_id':'wrong-plan','plan_id':'plan-does-not-exist'})
    assert pb.attribution(r['id'],p['plan_id'],11,START)['paper']['trade_count']==0


def test_manual_invalidation_preserves_rules_but_ends_current_plan(timebox,tmp_path):
    store=Store(tmp_path);w,r,d,p=published(store)
    timebox[0]=END;e=future(store,losing_path())
    pb=PlanBook(store);payload=dict(request_id='invalid',run_id=r['id'],plan_id=p['plan_id'],kind='invalidated',as_of=END,source='用户核对公告',note='假说已失效')
    assert pb.record(payload)==pb.record(payload)
    assert not w.get_run(r['id'])['decision']['plans'][0]['active']
    assert w.plan_outcomes(r['id'],[e],END)[0]['status']=='terminated_counterfactual'
    assert not w._active_plans()


def packet():
    fact=dict(id='company-fact',statement='合成公告事实',material=True,source_url='https://example.invalid/notice',
              source_published_at='2026-09-21T10:00:00+08:00',observed_at='2026-09-21T15:30:00+08:00',
              snapshot='用于接口验收的原始摘录',locator='正文第二段',conflict='none',verification='source_checked',first_hand=True,
              valid_until='2026-09-24T23:00:00+08:00',effective_period='本次合成测试期间',confidence='high')
    event={**fact,'id':'scheduled','status':'scheduled','event_at':'2026-09-24T18:00:00+08:00'}
    return dict(schema='decision-packet-v1',request_id='packet',message_id='message-1',source_reference='https://chatgpt.com/c/test',
                as_of=START,valid_until='2026-09-24T23:00:00+08:00',session='close',text='完整情报正文',facts=[fact],events=[event],
                coverage={c:{'state':'present','material':True,'reason':'已核对测试事实','refs':['company-fact']} for c in CATEGORIES},
                calendar={'windows_days':[1,3,7,30],'refreshed_at':START},comparison={'mode':'reconstructed','reason':'公开午间信息重建'},
                holding_state='unconfirmed',baseline_state='baseline_unknown')


def test_packet_source_bound_immutable_future_event_not_missing(timebox,tmp_path):
    store=Store(tmp_path);v=packet()
    update_settings(store,{'channels':{'trading':{'mode':'manual','name':'指定情报','reference':v['source_reference']}}})
    assert validate_packet(v,START)['ready_state']=='READY'
    e=import_packet(store,v)
    assert import_packet(store,{**v,'request_id':'retry'})['id']==e['id']
    with pytest.raises(ConflictError):import_packet(store,{**v,'text':'改写旧消息'})
    assert latest_packet(store,END)['ready_state']=='READY_WITH_GAPS'
    assert latest_packet(store,'2026-09-24T19:00:00+08:00')['gaps']==[]
    bad=copy.deepcopy(v);bad['facts'][0]['source_url']='turn1search0'
    assert any('待核实' in x for x in validate_packet(bad,START)['warnings'])
    bad=copy.deepcopy(v);bad['facts'][0]['first_hand']=False
    assert validate_packet(bad,START)['gaps']==[]
    bad['facts'][0]['cross_sources']=[{'url':'https://example.invalid/reprint','snapshot':'转载'}]
    assert validate_packet(bad,START)['gaps']==[]


def test_packet_accepts_only_registered_sibling_source(timebox,tmp_path):
    store=Store(tmp_path);v=packet()
    main=v['source_reference'];sibling='https://chatgpt.com/c/midday'
    update_settings(store,{'channels':{'trading':{'name':'指定情报','reference':main,
                      'additional_references':[sibling]},'market':{'name':'同源行情','reference':main}}})
    v['source_reference']=sibling
    e=import_packet(store,v)
    assert e['url']==sibling and e['metadata']['packet']['source_reference']==sibling
    assert import_packet(store,v)['id']==e['id']
    with pytest.raises(DomainError):
        import_packet(store,{**v,'message_id':'foreign','source_reference':'https://chatgpt.com/c/foreign'})
    with pytest.raises(DomainError):
        update_settings(store,{'channels':{'trading':{'name':'指定情报','reference':main,'additional_references':['javascript:bad']}}})


def test_conversation_url_cannot_impersonate_original_or_cross_source(timebox):
    v=packet();v['facts'][0]['source_url']=v['source_reference']
    result=validate_packet(v,START)
    assert v['source_reference'] not in result['source_groups']
    assert any('待核实' in x for x in result['warnings'])
    v=packet();v['facts'][0]['first_hand']=False
    v['facts'][0]['cross_sources']=[{'url':v['source_reference'],'snapshot':'对话转述'}]
    assert v['source_reference'] not in validate_packet(v,START)['source_groups']


def test_late_packet_catches_up_blocked_slot_without_duplicate_or_early_call(timebox,tmp_path,monkeypatch):
    import app.runner as runner
    store=Store(tmp_path);v=packet()
    update_settings(store,{'watchlist':[{'symbol':'601398'}],'channels':{
        'trading':{'name':'指定情报','reference':v['source_reference']},
        'market':{'name':'同源行情','reference':v['source_reference']}}})
    timebox[0]='2026-09-21T19:02:00+08:00'
    original=runner.create_job(store,runner.scheduled_request())
    original.update(status='blocked',error='行情未到达')
    store.write(f"automation/jobs/{original['id']}.json",original,overwrite=True)
    launches=[]
    monkeypatch.setattr(runner,'launch',lambda s,j: launches.append(j))
    assert runner.catch_up(store)['status']=='waiting_for_inputs' and not launches
    v['market']={'quotes':[dict(symbol='601398',price=10,previous_close=9.9,open=9.9,high=10.2,low=9.8,
        volume=1000,volume_unit='shares',amount=10000,amount_unit='CNY',as_of='2026-09-21T15:00:00+08:00',
        phase='closed',data_timing='closing_snapshot',price_source='https://example.invalid/quote')],
        'daily':{'601398':{'adjustment':'none','volume_unit':'shares','rows':[bar('2026-09-21',9.9,10.2,9.8,10)]}}}
    import_packet(store,v)
    out=runner.catch_up(store)
    assert out['status']=='queued' and len(launches)==1
    assert launches[0]['catch_up_for']==original['id']
    assert runner.catch_up(store)['status']=='already_attempted' and len(launches)==1
    assert store.read(f"automation/jobs/{original['id']}.json")['status']=='blocked'
    timebox[0]='2026-09-22T12:52:00+08:00'
    assert runner.catch_up(store)['status']=='waiting_for_inputs' and len(launches)==1
    done=store.read(f"automation/jobs/{out['job_id']}.json");done['status']='completed'
    store.write(f"automation/jobs/{done['id']}.json",done,overwrite=True)
    assert runner.catch_up(store)['status']=='not_needed' and len(launches)==1


def test_market_absolute_fields_lunch_close_and_costs(timebox,tmp_path):
    store=Store(tmp_path);q=put_market(store,'m',available_at=START,quote_as_of='2026-09-21T11:30:00+08:00',quote_phase='intraday')
    q['metadata']['daily']={}
    assert market_readiness(q,['601398'],'2026-09-21T13:00:00+08:00')['ready_state']=='READY_WITH_GAPS'
    assert market_readiness(q,['601398'],'2026-09-21T15:01:00+08:00')['ready_state']=='NOT_READY'
    q['metadata']['quotes'][0].pop('amount')
    quality=market_readiness(q,['601398'],'2026-09-21T13:05:00+08:00')
    assert quality['gaps']==[] and any('成交额' in x for x in quality['warnings'])
    timebox[0]=END
    payload=dict(request_id='cost',start_date='2026-09-21',end_date='2026-09-23',amount=12,source='测试账单',confirmed_complete=True)
    record_costs(store,payload)
    with pytest.raises(ConflictError):record_costs(store,{**payload,'request_id':'overlap'})
    with pytest.raises(DomainError):record_costs(store,{**payload,'request_id':'today','end_date':'2026-09-24'})
    ledger={'baseline':{'as_of':START},'as_of':'2026-09-23T23:59:59+08:00','net_profit':20,'incremental_profit':10}
    assert summary(store,ledger)['profit_after_operating_costs']==8
    assert summary(store,{**ledger,'as_of':END})['profit_after_operating_costs'] is None


def test_new_endpoints_keep_csrf_and_return_actionable_errors(timebox,tmp_path):
    store=Store(tmp_path)
    with TestClient(create_app(store,testing=True)) as c:
        state=c.get('/api/state').json()
        assert 'paper_ledger' in state and 'performance' in state and 'packet_status' in state
        assert c.post('/api/packets/import',json={}).status_code==403
        headers={'x-csrf-token':state['system']['csrf_token']}
        assert c.post('/api/packets/import',json={},headers=headers).status_code==400
        assert c.post('/api/plans/events',json={},headers=headers).status_code==400
        assert c.post('/api/ledger/paper/baseline',headers=headers,json=dict(request_id='paper',confirmed=True,as_of=START,cash=7000,positions=[])).status_code==200
        assert c.get('/api/state').json()['ledger']['confirmed'] is False


def test_packet_to_research_preserves_absolute_prices_and_unknown_account(timebox,tmp_path):
    from app.runner import Runner, create_job
    store=Store(tmp_path);value=packet()
    update_settings(store,{'watchlist':[{'symbol':'601398'}],'channels':{
        'trading':{'mode':'manual','name':'指定情报','reference':value['source_reference']},
        'market':{'mode':'manual','name':'同一情报内行情','reference':value['source_reference']}}})
    value['market']={'quotes':[dict(symbol='601398',price=10,previous_close=9.9,open=9.9,high=10.2,low=9.8,
        volume=1000,volume_unit='shares',amount=10000,amount_unit='CNY',as_of='2026-09-21T15:00:00+08:00',
        phase='closed',data_timing='closing_snapshot',price_source='https://example.invalid/quote')],
        'daily':{'601398':{'adjustment':'none','volume_unit':'shares','rows':[bar('2026-09-21',9.9,10.2,9.8,10)]}}}
    ev=import_packet(store,value)
    assert ev['metadata']['market_evidence_id']
    class NeverCall:
        def complete(self,*a,**k):raise AssertionError('mandatory packet must not require a billable selection call')
    runner=Runner(store,NeverCall());runner.job=create_job(store,'packet-round')
    request=runner.inputs()
    run=Workflow(store).prepare(request)
    assert run['account_snapshot']['confirmed'] is False
    assert set(run['evidence_ids'])=={ev['id'],ev['metadata']['market_evidence_id']}
    q=next(e for e in run['evidence'] if e['kind']=='market')['metadata']['quotes'][0]
    assert q['amount']==10000 and q['change_pct']==pytest.approx(1.010101)
    with pytest.raises(DomainError,match='结构情报包'):
        Workflow(store).prepare({**request,'request_id':'bypass','evidence_ids':[ev['metadata']['market_evidence_id']]})
    value['facts'][0]['valid_until']='2026-09-21T16:00:00+08:00'
    assert validate_packet(value,START)['gaps']==[]


def minimal_packet(store, quotes):
    reference='https://chatgpt.com/c/minimal'
    update_settings(store,{'watchlist':[{'symbol':'601398'},{'symbol':'510300'},{'symbol':'588290'}],
        'channels':{'trading':{'name':'情报','reference':reference},
                    'market':{'name':'同源行情','reference':reference}}})
    return dict(schema='research-packet-v2',request_id='minimal',message_id='minimal',
                source_reference=reference,as_of=START,session='close',
                text='行情和公司事件原文 https://example.invalid/notice',market={'quotes':quotes})


def minimal_quote(symbol='601398',price=10):
    return dict(symbol=symbol,name=symbol,price=price,as_of='2026-09-21T15:00:00+08:00',
                phase='closed',price_source='https://example.invalid/quote')


def test_minimal_delivery_prepares_research_and_makes_partial_scope_explicit(timebox,tmp_path):
    from app.runner import Runner,create_job
    store=Store(tmp_path)
    value=minimal_packet(store,[minimal_quote(),{**minimal_quote('510300'),'as_of':'2026-09-22T15:00:00+08:00'}])
    ev=import_packet(store,value)
    assert ev['text']==value['text']
    assert any('510300' in x for x in latest_packet(store)['warnings'])
    class NeverCall:
        def complete(self,*a,**k):raise AssertionError('input preparation must not call model')
    runner=Runner(store,NeverCall());runner.job=create_job(store,'minimal-round')
    run=Workflow(store).prepare(runner.inputs())
    assert run['symbols']==['601398'] and run['account_snapshot']['confirmed'] is False
    assert [x['symbol'] for x in run['research_brief']['data_scope']['unavailable_symbols']]==['510300','588290']
    assert any('510300' in x for x in run['warnings'])
    q=next(x for x in run['evidence'] if x['kind']=='market')['metadata']['quotes'][0]
    assert q['price']==10 and q['volume'] is None and q['amount'] is None


def test_local_primary_works_without_web_and_late_web_cannot_replace_it(timebox,tmp_path,monkeypatch):
    from app.runner import Runner,create_job
    store=Store(tmp_path)
    market=put_market(store,'direct',available_at='2026-09-21T15:30:00+08:00',
                      quote_as_of='2026-09-21T15:00:00+08:00',rows=[bar('2026-09-21',10,10,10,10)])
    reference='codex-automation:a';web='https://chatgpt.com/c/supplement'
    update_settings(store,{'watchlist':[{'symbol':'601398'}],
        'channels':{'trading':{'name':'本地采集','reference':reference,'additional_references':[web]}}})
    value=dict(schema='research-packet-v2',request_id='local',message_id='local',source_reference=reference,
               as_of=START,session='close',text='本地直接读取的公告 https://example.invalid/notice')
    primary=import_packet(store,value)
    monkeypatch.setattr('app.market.collect',lambda *a,**k:store.read('evidence/direct.json'))
    class NeverCall:
        def complete(self,*a,**k):raise AssertionError('web absence must not require a model for inputs')
    runner=Runner(store,NeverCall());runner.job=create_job(store,'local-inputs')
    prepared=Workflow(store).prepare(runner.inputs())
    assert prepared['symbols']==['601398']
    assert primary['id'] in [e['id'] for e in prepared['evidence']]
    timebox[0]='2026-09-21T16:40:00+08:00'
    supplement=import_packet(store,{**value,'request_id':'web','message_id':'web','source_reference':web,
        'as_of':timebox[0],'market':{'quotes':[minimal_quote(price=999)]}})
    assert supplement['metadata']['market_evidence_id'] is None
    assert latest_packet(store)['evidence_id']==primary['id']
    assert latest_packet(store)['source_role']=='primary'
    with pytest.raises(DomainError,match='绑定'):
        import_packet(store,{**value,'message_id':'foreign','source_reference':'codex-automation:other'})
    bad={**value,'facts':[{'id':'fake','source_url':reference}]}
    assert any('底层来源' in x for x in validate_packet(bad,START)['warnings'])


def test_empty_prices_keep_body_but_prevent_unpriced_model_run(timebox,tmp_path):
    from app.runner import Runner,create_job,InputBlocked
    store=Store(tmp_path);value=minimal_packet(store,[])
    ev=import_packet(store,value)
    assert ev['metadata']['market_evidence_id'] is None and latest_packet(store)['packet']['text']==value['text']
    class NeverCall:
        def complete(self,*a,**k):raise AssertionError('no priced symbol means no model call')
    runner=Runner(store,NeverCall());runner.job=create_job(store,'empty-round')
    with pytest.raises(InputBlocked,match='主价格'):runner.inputs()


def test_auxiliary_conflicts_do_not_discard_good_price_but_main_conflicts_do(timebox,tmp_path):
    store=Store(tmp_path)
    value=minimal_packet(store,[{**minimal_quote(),'open':10,'low':11,'high':12},minimal_quote('510300',4)])
    value['market']['daily']={'510300':{'adjustment':'none','rows':[bar('2026-09-21',4,4.2,3.8,4.1)]}}
    ev=import_packet(store,value)
    market=store.read(f"evidence/{ev['metadata']['market_evidence_id']}.json")
    assert [q['symbol'] for q in market['metadata']['quotes']]==['601398']
    assert market['metadata']['quotes'][0]['price']==10
    assert any('辅助行情' in x for x in ev['metadata']['warnings'])
    assert any('主价格' in x for x in ev['metadata']['warnings'])
    value.update(request_id='malformed-history',message_id='malformed-history')
    value['market']={'quotes':[minimal_quote()], 'daily':{'601398':None}}
    ev=import_packet(store,value)
    assert ev['metadata']['market_evidence_id']


def test_quote_time_and_optional_daily_volume_have_independent_effects(timebox,tmp_path):
    from app.market import latest_market
    store=Store(tmp_path);value=minimal_packet(store,[minimal_quote()])
    value['market']['daily']={'601398':{'adjustment':'none','rows':[dict(bar('2026-09-21',10,10,10,10),volume=None)]}}
    ev=import_packet(store,value)
    market=store.read(f"evidence/{ev['metadata']['market_evidence_id']}.json")
    assert market['metadata']['daily']['601398']['rows'][0]['volume'] is None
    assert market_readiness(market,['601398'],START)['usable_symbols']==['601398']
    assert market_readiness(market,['601398'],'2026-09-22T13:00:00+08:00')['usable_symbols']==[]
    timebox[0]='2026-09-22T12:50:00+08:00'
    value.update(request_id='midday',message_id='midday',as_of=timebox[0],session='midday')
    value['market']={'quotes':[{**minimal_quote(),'as_of':'2026-09-22T11:30:00+08:00','phase':'intraday'}]}
    import_packet(store,value)
    assert not latest_market(store,'2026-09-22T13:05:00+08:00')['metadata']['quotes'][0]['stale']
    assert latest_market(store,'2026-09-22T15:01:00+08:00')['metadata']['quotes'][0]['stale']
    with pytest.raises(DomainError):validate_packet({**value,'as_of':'2026-09-23T10:00:00+08:00'},timebox[0])
    with pytest.raises(DomainError):validate_packet({**value,'text':''},timebox[0])


def test_superseding_with_different_horizon_and_monthly_cost_math(timebox,tmp_path):
    store=Store(tmp_path);w,r,d,p=published(store,horizon=3)
    _,new,_,_=published(store,horizon=5,supersedes=r['id'],request='revised')
    assert [x['run_id'] for x in w._active_plans()]==[new['id']]
    assert w.get_run(r['id'])['decision']['plans'][0]['superseded_by']==new['id']
    assert w.plan_outcomes(r['id'],[],START)[0]['status']=='terminated_counterfactual'
    record_costs(store,dict(request_id='august-cost',start_date='2026-08-01',end_date='2026-08-31',amount=20,source='合成月账单',confirmed_complete=True))
    ledger={'baseline':{'as_of':'2026-08-01T09:00:00+08:00'},'as_of':START,'net_profit':160,
            'complete_months':[{'month':'2026-08','net_profit':160}]}
    assert summary(store,ledger)['monthly_average_after_operating_costs']==140
    assert summary(store,ledger)['stability_proven'] is False
