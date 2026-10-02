"""Exercise the web/CLI boundary for shared discussions and source browsing."""
import json
import hashlib

from fastapi.testclient import TestClient

from app.cli import main
from app.evidence import add_text
from app.server import create_app
from app.store import Store, now_iso
from app.workflow import Workflow
from app.settings import update_settings


def test_discussion_routes_preserve_independent_drafts(tmp_path, capsys):
    store = Store(tmp_path)
    update_settings(store, {"channels": {"market": {"mode": "manual", "name": "测试指定渠道"}}})
    evidence = add_text(store, dict(request_id='source', source_id='caixin', title='测试', text='可核对原文'))
    workflow = Workflow(store)
    run = workflow.prepare(dict(request_id='run', decision_contract=2, as_of=now_iso(), mode='live', symbols=['601398'], evidence_ids=[evidence['id']]))
    opinion = dict(summary='独立初稿', evidence_ids=[evidence['id']], claims=['证据限定的主张'], counterevidence=[], unknowns=['待确认'], changes=[])
    workflow.submit_role(run['id'], 'opportunity', 'initial', opinion)
    with TestClient(create_app(store, testing=True)) as client:
        url = f"/api/runs/{run['id']}/discussion"
        assert client.get(url).json()['messages'] == []
        assert client.get(url, params={'role': 'counter'}).json()['messages'] == []
        assert len(client.get(url, params={'role': 'opportunity'}).json()['messages']) == 1
        assert client.get(url, params={'role': 'invalid'}).status_code == 400
        workflow.submit_role(run['id'], 'counter', 'initial', opinion)
        shared = client.get(url).json()
        assert shared['delivery'] == 'pull'
        assert {m['sender'] for m in shared['messages']} == {'opportunity', 'counter'}
        assert all('judge' in m['recipients'] for m in shared['messages'])
    assert main(['--data-dir', str(tmp_path), 'discussion', run['id'], '--role', 'judge']) == 0
    assert len(json.loads(capsys.readouterr().out)['messages']) == 2


def test_search_route_and_cli_use_same_filter_and_pagination(tmp_path, capsys):
    store = Store(tmp_path)
    for number, source in enumerate(['caixin', 'caixin', 'icbc']):
        add_text(store, dict(request_id=f'article-{number}', source_id=source, title=f'银行报道{number}', text='真实测试正文'))
    with TestClient(create_app(store, testing=True)) as client:
        result = client.get('/api/materials/search', params={'q': '银行', 'source_id': 'caixin', 'limit': 1}).json()
        assert result['total'] == 2 and len(result['items']) == 1
        second = client.get('/api/materials/search', params={'q': '银行', 'source_id': 'caixin', 'limit': 1, 'offset': 1}).json()
        assert second['items'][0]['id'] != result['items'][0]['id']
        assert client.get('/api/materials/search', params={'limit': 101}).status_code == 422
    assert main(['--data-dir', str(tmp_path), 'search', '--query', '银行', '--source', 'caixin', '--limit', '1', '--offset', '1']) == 0
    assert json.loads(capsys.readouterr().out)['items'][0]['id'] == second['items'][0]['id']


def test_local_caixin_http_sync_search_detail_and_retry(tmp_path, monkeypatch):
    import app.caixin as caixin

    raw_root = tmp_path / 'raw'
    source_root = tmp_path / 'source'
    raw_root.mkdir()
    source_root.mkdir()
    body = '银行测试正文。\n\n本段只用于隔离接口验收。'
    raw_bytes = body.encode('utf-8')
    (raw_root / 'article.html').write_bytes(raw_bytes)
    document = dict(
        metadata=dict(platform='caixin', stable_id='caixin:123', article_id='123', title='财新隔离测试',
                      published_at='2026-09-01T00:00:00+00:00', source_url='https://example.invalid/123',
                      extraction_status='complete', body_completeness='complete', pagination_completeness='complete',
                      body_integrity={'status': 'complete'}, raw_paths=['article.html'],
                      raw_sha256=[hashlib.sha256(raw_bytes).hexdigest()], category='金融'),
        faithful_body=body, faithful_body_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )
    (source_root / 'article.json').write_text(json.dumps(document), encoding='utf-8')
    monkeypatch.setattr(caixin, 'DEFAULT_CAIXIN_ROOT', source_root)
    monkeypatch.setattr(caixin, 'DEFAULT_CAIXIN_RAW_ROOT', raw_root)
    store = Store(tmp_path / 'data')
    from app.settings import update_settings
    update_settings(store, {"channels": {"caixin_enabled": True}})
    with TestClient(create_app(store, testing=True)) as client:
        headers = {'x-csrf-token': client.get('/api/state').json()['system']['csrf_token']}
        assert client.get('/api/sources/caixin').json()['last_sync'] is None
        assert client.post('/api/sources/caixin/sync', json={'request_id': 'one'}).status_code == 403
        first = client.post('/api/sources/caixin/sync', json={'request_id': 'one'}, headers=headers).json()
        assert first['created'] == 1 and first['ready'] == 1 and first['blocked'] == 0
        assert client.post('/api/sources/caixin/sync', json={'request_id': 'one'}, headers=headers).json() == first
        again = client.post('/api/sources/caixin/sync', json={'request_id': 'two'}, headers=headers).json()
        assert again['created'] == 0 and again['existing'] == 1
        state = client.get('/api/sources/caixin').json()
        assert state['last_request_id'] == 'two' and state['library']['version_count'] == 1
        found = client.get('/api/materials/search', params={'q': '银行', 'source_id': 'caixin', 'category': '金融'}).json()
        assert found['total'] == 1
        detail = client.get('/api/evidence/' + found['items'][0]['id']).json()
        assert detail['text'] == body and detail['metadata']['paragraphs'][1]['paragraph'] == 2
        assert detail['available_at'] != detail['published_at']
        assert (source_root / 'article.json').read_text(encoding='utf-8') == json.dumps(document)
