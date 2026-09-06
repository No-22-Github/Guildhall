import json
import os
import tomllib
from fastapi.testclient import TestClient
from guildhall.api import AppContext, build_app
from guildhall.config import Config, load


def client_for(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.toml').write_text('''# preserve me
[agent]
name = "claude"
[agent.receptionist]
model = "old"
auth_token = "secret-original"
[server]
port = 9999
[custom]
keep = "yes"
''')
    return TestClient(build_app(AppContext(load())))


def payload(data):
    return {'revision': data['revision'], 'roles': {k: {'model': v['model'], 'base_url': v['base_url'], 'auth_token': None, 'clear_token': False} for k, v in data['roles'].items()}}


def test_save_preserves_secret_comments_other_sections_and_requires_restart(tmp_guildhall):
    c = client_for(tmp_guildhall)
    original = c.get('/api/settings').json()
    assert 'secret-original' not in json.dumps(original)
    assert original['roles']['receptionist']['has_token']
    assert not original['restart_required']
    body = payload(original)
    body['roles']['receptionist']['model'] = 'new-model'
    result = c.put('/api/settings', json=body)
    assert result.status_code == 200
    assert result.json()['restart_required']
    assert 'secret-original' not in result.text
    text = (tmp_guildhall / 'config.toml').read_text()
    assert '# preserve me' in text
    doc = tomllib.loads(text)
    assert doc['agent']['receptionist']['auth_token'] == 'secret-original'
    assert doc['server']['port'] == 9999 and doc['custom']['keep'] == 'yes'
    assert os.stat(tmp_guildhall / 'config.toml').st_mode & 0o777 == 0o600
    assert c.put('/api/settings', json=body).status_code == 409


def test_token_replace_clear_validation_and_origin(tmp_guildhall):
    c = client_for(tmp_guildhall)
    body = payload(c.get('/api/settings').json())
    body['roles']['appraiser']['auth_token'] = 'replacement-secret'
    result = c.put('/api/settings', json=body)
    assert result.status_code == 200 and 'replacement-secret' not in result.text
    assert load().role_config('appraiser').auth_token == 'replacement-secret'
    body = payload(result.json())
    body['roles']['receptionist']['clear_token'] = True
    result = c.put('/api/settings', json=body)
    assert result.status_code == 200
    assert load().role_config('receptionist').auth_token is None
    body = payload(result.json())
    body['roles']['appraiser']['base_url'] = 'https://user:replacement-secret@example.com'
    invalid = c.put('/api/settings', json=body)
    assert invalid.status_code == 422 and 'replacement-secret' not in invalid.text
    assert c.put('/api/settings', json=body, headers={'origin': 'https://evil.example'}).status_code == 403
    assert c.get('/api/settings', headers={'origin': 'https://evil.example'}).status_code == 403
    body['roles']['appraiser']['base_url'] = 'http://127.0.0.1:8080/anthropic'
    assert c.put('/api/settings', json=body).status_code == 200
