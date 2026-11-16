"""Goal defaults use a narrow plugin settings route; existing Goal records stay untouched."""
import asyncio
import hashlib
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
import httpx
import pytest
import yaml

from plugins.ide.goal_settings import add_routes


@pytest.fixture
def settings_app(tmp_path, monkeypatch):
    from supervisor import admin_api
    monkeypatch.setattr(admin_api, '_admin_token', 'settings-test')
    monkeypatch.setattr(admin_api, '_web_auth', None)
    app = FastAPI()
    app.state.state = SimpleNamespace(execution_runtime=SimpleNamespace(workspace_root=tmp_path))
    router = APIRouter()
    add_routes(router)
    app.include_router(router, prefix='/v1/plugins/ide')
    return app, tmp_path


def test_defaults_patch_preserves_unrelated_runtime_and_existing_goal(settings_app):
    app, root = settings_app
    path = root / 'config/runtime.yaml'
    path.parent.mkdir()
    original = {'engine': {'model': 'model-a'}, 'plugins': {'ide': {'other': 42}, 'another': {'enabled': True}}}
    path.write_text(yaml.safe_dump(original))
    goal = root / 'data/ide/goals/sessions/existing.json'
    goal.parent.mkdir(parents=True)
    goal.write_text('{"max_rounds":8,"rounds":3}')
    before = goal.read_bytes()

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers={'Authorization': 'Bearer settings-test'}) as client:
            response = await client.get('/v1/plugins/ide/goal/settings')
            assert response.status_code == 200
            initial = response.json()
            assert initial['default_max_rounds'] == 256
            assert initial['revision'] == hashlib.sha256(path.read_bytes()).hexdigest()
            body = {'default_max_rounds': 512, 'expected_revision': initial['revision']}
            response = await client.put('/v1/plugins/ide/goal/settings', json=body)
            assert response.status_code == 200
            assert response.json()['default_max_rounds'] == 512
            saved = yaml.safe_load(path.read_text())
            assert saved['engine'] == original['engine']
            assert saved['plugins']['another'] == original['plugins']['another']
            assert saved['plugins']['ide'] == {'other': 42, 'goal': {'default_max_rounds': 512}}
            assert goal.read_bytes() == before
            durable = path.read_bytes()
            response = await client.put('/v1/plugins/ide/goal/settings', json=body)
            assert response.status_code == 409
            assert path.read_bytes() == durable
    asyncio.run(run())


@pytest.mark.parametrize('value', [None, True, False, 0, -1, 1.5, '256', 1000001])
def test_invalid_values_never_write(settings_app, value):
    app, root = settings_app
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers={'Authorization': 'Bearer settings-test'}) as client:
            initial = (await client.get('/v1/plugins/ide/goal/settings')).json()
            response = await client.put('/v1/plugins/ide/goal/settings', json={'default_max_rounds': value, 'expected_revision': initial['revision']})
            assert response.status_code == 422
            assert not (root / 'config/runtime.yaml').exists()
    asyncio.run(run())


def test_malformed_existing_settings_are_not_overwritten(settings_app):
    app, root = settings_app
    path = root / 'config/runtime.yaml'
    path.parent.mkdir()
    path.write_text('plugins: []\n')
    original = path.read_bytes()
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers={'Authorization': 'Bearer settings-test'}) as client:
            assert (await client.get('/v1/plugins/ide/goal/settings')).status_code == 422
            assert path.read_bytes() == original
    asyncio.run(run())


def test_settings_panel_is_declared_by_plugin_without_host_goal_branch():
    from plugins.ide import PLUGIN_META
    panel = next(p for p in PLUGIN_META['web']['panels'] if p['id'] == 'goal_settings')
    assert panel['slot'] == 'settings'
    assert panel['entry'] == '/v1/plugins/ide/web/goal_settings.html'


@pytest.mark.parametrize('raw', [
    '# keep header\nengine:\n  streaming: true # keep inline\n',
    'plugins:\n  ide:\n    goal:\n      default_max_rounds: 8 # preserve\n    other: yes\nengine: {}\n',
    'plugins: {ide: {goal: {default_max_rounds: 8}}} # preserve\n',
    'plugins:\n  ide: {} # preserve\nengine: {}\n',
])
def test_scalar_patch_preserves_comments_and_other_values(raw):
    from supervisor.runtime_config import patch_integer
    changed = patch_integer(raw, ('plugins', 'ide', 'goal', 'default_max_rounds'), 512)
    for line in raw.splitlines():
        if '#' in line:
            assert line[line.index('#'):] in changed
    assert yaml.safe_load(changed)['plugins']['ide']['goal']['default_max_rounds'] == 512


def test_settings_auth_and_bad_body(settings_app):
    app, root = settings_app
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for method in ['GET', 'PUT']:
                assert (await client.request(method, '/v1/plugins/ide/goal/settings')).status_code == 401
            client.headers['Authorization'] = 'Bearer settings-test'
            for body in [b'{', b'null', b'[]', b'\xff']:
                assert (await client.put('/v1/plugins/ide/goal/settings', content=body)).status_code == 422
            assert (await client.put('/v1/plugins/ide/goal/settings', content=b'x' * 4097)).status_code == 413
            assert not (root / 'config/runtime.yaml').exists()
    asyncio.run(run())


def test_runtime_raw_edit_invalidates_goal_settings_revision(settings_app):
    from supervisor.admin_api import create_admin_router
    app, root = settings_app
    app.include_router(create_admin_router(root), prefix='/v1/admin')
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers={'Authorization': 'Bearer settings-test'}) as client:
            original = (await client.get('/v1/plugins/ide/goal/settings')).json()
            edited = '# external edit\nengine:\n  streaming: false\n'
            assert (await client.put('/v1/admin/runtime/raw', json={'content': edited})).status_code == 200
            response = await client.put('/v1/plugins/ide/goal/settings', json={'default_max_rounds': 512, 'expected_revision': original['revision']})
            assert response.status_code == 409
            assert (root / 'config/runtime.yaml').read_text() == edited
    asyncio.run(run())


def test_raw_writer_and_settings_share_one_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from plugins.ide import goal_settings
    from supervisor import runtime_config
    assert goal_settings.WRITE_LOCK is runtime_config.WRITE_LOCK
    started = threading.Event()
    def save():
        started.set()
        runtime_config.write_raw(tmp_path, '# concurrent editor\nengine: {}\n')
    with ThreadPoolExecutor(max_workers=1) as pool:
        with goal_settings.WRITE_LOCK:
            pending = pool.submit(save)
            assert started.wait(2)
            assert not pending.done()
            assert not (tmp_path / 'config/runtime.yaml').exists()
        pending.result(timeout=2)
    assert (tmp_path / 'config/runtime.yaml').read_text().startswith('# concurrent editor')
