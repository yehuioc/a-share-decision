import copy
import io
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from app.evidence import add_file, add_text, add_check, source_scores, _page_readable
from app.market import parse_quotes, fetch_daily, collect, latest_market
from app.server import create_app
from app.store import Store, DomainError, ConflictError
from app.settings import update_settings


def meta(request_id='article'):
    return dict(request_id=request_id, title='测试材料', source_id='caixin')


def test_originals_pdf_failure_and_idempotent_concurrent_import(tmp_path):
    s = Store(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: add_file(s, meta(), 'article.txt', '可读原文'.encode()), range(2)))
    assert results[0] == results[1]
    assert s.path(results[0]['metadata']['original_relative_path']).read_bytes() == '可读原文'.encode()
    with pytest.raises(ConflictError):
        add_file(s, meta(), 'article.txt', b'changed')
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    stream = io.BytesIO()
    writer.write(stream)
    empty = add_file(s, meta('scan'), 'scan.pdf', stream.getvalue())
    assert empty['status'] == 'needs_text'
    assert empty['metadata']['pages'][0]['page'] == 1
    assert empty['metadata']['warnings']
    with pytest.raises(DomainError):
        add_file(s, meta('broken'), 'broken.pdf', b'not a PDF')
    assert _page_readable('中国工商银行2026半年度报告 ICBC Interim Report')
    assert not _page_readable('字体错误' + 'ᆴཟಆ౯ӑᄡუ' * 20)


def test_text_pdf_preserves_page_numbers_and_original_bytes(tmp_path):
    writer = PdfWriter()
    for number in (1, 2):
        page = writer.add_blank_page(width=200, height=200)
        font = DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        stream = DecodedStreamObject()
        stream.set_data(f'BT /F1 12 Tf 10 100 Td (Page {number} extraction fixture) Tj ET'.encode())
        page[NameObject('/Contents')] = stream
    output = io.BytesIO(); writer.write(output)
    s = Store(tmp_path)
    result = add_file(s,meta('text-pdf'),'text.pdf',output.getvalue())
    assert result['status'] == 'ready'
    assert result['metadata']['extraction_complete']
    assert result['metadata']['pages'][1]['page'] == 2
    assert 'Page 2 extraction fixture' in result['metadata']['pages'][1]['text']
    assert s.path(result['metadata']['original_relative_path']).read_bytes() == output.getvalue()


def test_source_scores_are_separate_and_replay_not_prediction(tmp_path):
    s = Store(tmp_path)
    e = add_text(s, {**meta(), 'text': '一个待核验的事实'})
    with pytest.raises(DomainError):
        add_check(s, dict(request_id='bad', source_id='caixin', claim='事实', verdict='correct'))
    check = dict(request_id='good', source_id='caixin', claim='事实', verdict='correct', evidence_ids=[e['id']])
    assert add_check(s, check) == add_check(s, check)
    obs = dict(symbol='601398', review_date='2026-09-28', status='resolved', direction_hit=False, return_pct=-1)
    frozen = copy.deepcopy(e)
    second_version = {**copy.deepcopy(e), 'id': 'caixin-second-version'}
    run = dict(id='r1', mode='live', evidence=[frozen, second_version], decision={'plans': [dict(symbol='601398', evidence_ids=[e['id'], second_version['id']])]}, reviews=[{'observations': [obs, obs]}])
    s.write(f"evidence/{e['id']}.json", {**e, 'source_id': 'manual'}, overwrite=True)
    scores = {x['id']: x for x in source_scores(s, [run, {**run, 'id': 'replay', 'mode': 'replay'}])}
    assert scores['caixin']['facts']['accuracy'] == 1
    assert scores['caixin']['prediction']['samples'] == 1
    assert scores['caixin']['prediction']['hit_rate'] == 0
    assert scores['manual']['prediction']['samples'] == 0
    assert scores['caixin']['overall_score'] is None
    assert scores['caixin']['automatic_weight'] is False
    assert scores['caixin']['timeliness']['news_acquisition']['samples'] == 0
    assert scores['icbc']['facts']['accuracy'] is None
    assert scores['icbc']['prediction']['hit_rate'] is None


def quote_text(timestamp):
    fields = ['0'] * 35
    for i, v in {1:'工商银行', 2:'601398', 3:'7.11', 4:'7.09', 5:'7.10', 6:'200', 30:timestamp, 33:'7.13', 34:'7.08'}.items():
        fields[i] = v
    return 'v_sh601398="' + '~'.join(fields) + '";'


def test_quote_calendar_intraday_stale_and_future():
    quotes, errors = parse_quotes(quote_text('20260924150000'), ['601398'], '2026-09-27T18:00:00+08:00')
    assert not errors and not quotes[0]['stale'] and quotes[0]['phase'] == 'closed'
    quotes, _ = parse_quotes(quote_text('20260924150000'), ['601398'], '2026-09-28T15:05:00+08:00')
    assert quotes[0]['stale']
    quotes, _ = parse_quotes(quote_text('20260928100000'), ['601398'], '2026-09-28T10:01:00+08:00')
    assert quotes[0]['phase'] == 'intraday'
    quotes, errors = parse_quotes(quote_text('20260929100000'), ['601398'], '2026-09-28T10:01:00+08:00')
    assert not quotes and errors
    quotes, errors = parse_quotes(quote_text('20260927100000'), ['601398'], '2026-09-27T12:00:00+08:00')
    assert not quotes and errors


def test_saved_quote_becomes_stale_without_overwriting_evidence(tmp_path):
    s = Store(tmp_path)
    quotes, _ = parse_quotes(quote_text('20260924150000'), ['601398'], '2026-09-27T18:00:00+08:00')
    e = dict(id='quote',kind='market',captured_at='2026-09-27T18:00:00+08:00',metadata={'quotes':quotes,'warnings':[]})
    s.write('evidence/quote.json',e)
    assert latest_market(s, '2026-09-28T15:01:00+08:00')['metadata']['quotes'][0]['stale']
    assert not s.read('evidence/quote.json')['metadata']['quotes'][0]['stale']


def test_provider_failure_and_adjustment_not_silently_substituted(tmp_path, monkeypatch):
    class AdjustedOnly:
        def raise_for_status(self): pass
        def json(self): return {'data': {'sh601398': {'qfqday': [['2026-09-24', 1, 1, 1, 1, 1]]}}}
    monkeypatch.setattr('app.market.requests.get', lambda *a, **k: AdjustedOnly())
    with pytest.raises(DomainError, match='qfqday'):
        fetch_daily('601398', '2026-09-24')
    def unavailable(*a, **k): raise requests.Timeout('test timeout')
    monkeypatch.setattr('app.market.requests.get', unavailable)
    store = Store(tmp_path)
    update_settings(store, {"channels": {"market": {"mode": "tencent", "name": "腾讯行情测试替身"}}})
    e = collect(store, 'offline', ['601398'])
    assert e['status'] == 'error' and len(e['metadata']['warnings']) == 2
    assert e['metadata']['daily'] == {}


def test_midday_direct_collection_keeps_price_but_excludes_unfinished_daily(tmp_path,monkeypatch):
    monkeypatch.setattr('app.market.now_iso',lambda:'2026-09-28T12:50:00+08:00')
    class QuoteResponse:
        content=quote_text('20260928113000').encode('utf-8')
        def raise_for_status(self):pass
    monkeypatch.setattr('app.market.requests.get',lambda *a,**k:QuoteResponse())
    monkeypatch.setattr('app.market.fetch_daily',lambda *a,**k:{'adjustment':'none','volume_unit':'lots','rows':[
        {'date':'2026-09-24','open':10,'high':10,'low':10,'close':10,'volume':100},
        {'date':'2026-09-28','open':10,'high':11,'low':9,'close':10,'volume':50}]})
    store=Store(tmp_path)
    update_settings(store,{'channels':{'market':{'mode':'tencent','name':'腾讯测试'}}})
    result=collect(store,'midday',['601398'])
    assert result['metadata']['quotes'][0]['phase']=='intraday'
    assert [r['date'] for r in result['metadata']['daily']['601398']['rows']]==['2026-09-24']
    assert not any('日线未更新' in x for x in result['metadata']['warnings'])


def test_http_real_routes_input_and_csrf(tmp_path):
    s = Store(tmp_path)
    with TestClient(create_app(s, testing=True)) as client:
        state = client.get('/api/state').json()
        assert state['ledger']['confirmed'] is False
        assert client.get('/').status_code == 200
        assert client.get('/static/app.js').status_code == 200
        assert client.get('/static/app.js').headers['content-type'].startswith('text/javascript')
        assert client.get('/static/style.css').headers['content-type'].startswith('text/css')
        headers = {'x-csrf-token': state['system']['csrf_token']}
        data = {**meta(), 'text': '<script>alert(1)</script>原始资料'}
        assert client.post('/api/materials/text', json=data).status_code == 403
        assert client.post('/api/materials/text', json=data, headers={**headers, 'origin': 'https://attacker.invalid'}).status_code == 403
        response = client.post('/api/materials/text', json=data, headers=headers)
        assert response.status_code == 200
        eid = response.json()['id']
        assert client.get('/api/evidence/' + eid).json()['text'] == data['text']
        bad = client.post('/api/materials/text', json={**data, 'text': 'changed'}, headers=headers)
        assert bad.status_code == 409 and bad.json()['detail']
        baseline = dict(request_id='baseline', confirmed=True, as_of='2026-09-01T09:00:00+08:00', cash=7000, positions=[], risk_budget=None)
        assert client.post('/api/ledger/baseline', json=baseline, headers=headers).status_code == 200
        assert client.get('/api/state').json()['ledger']['net_value'] == 7000
        assert 'frame-ancestors' in response.headers['content-security-policy']
        assert client.get('/api/state', headers={'host': 'attacker.invalid'}).status_code == 400


def test_user_report_visible_without_inventing_account_baseline(tmp_path):
    store = Store(tmp_path)
    profile = {'historical_positions': [{'symbol': '601398', 'quantity': 400}],
               'current_positions': [{'symbol': '601398', 'quantity': 200}],
               'reported_trades': [{'symbol': '601398', 'side': 'sell', 'quantity': 200,
                                    'price': 8.29, 'gross_amount': 1658, 'date': None}]}
    store.write('research_profile.json', profile)
    with TestClient(create_app(store, testing=True)) as client:
        result = client.get('/api/state').json()
    assert result['research_profile'] == profile
    assert result['ledger']['confirmed'] is False
    assert result['ledger']['positions'] == []
    assert store.read('ledger/baseline.json') is None
    assert store.list('ledger/events') == []
