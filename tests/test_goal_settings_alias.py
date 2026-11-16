"""A narrow Goal settings update must never alter aliased unrelated configuration."""
import asyncio

import httpx
import pytest

from supervisor.runtime_config import patch_integer
from test_goal_settings import settings_app


@pytest.mark.parametrize('raw', [
    'other: &shared\n  default_max_rounds: 256\nplugins:\n  ide:\n    goal: *shared\n',
    'other: &value 256\nplugins:\n  ide:\n    goal:\n      default_max_rounds: *value\n',
    'plugins:\n  ide:\n    goal: &shared\n      default_max_rounds: 256\nother: *shared\n',
])
def test_shared_goal_yaml_is_rejected_without_writing(settings_app, raw):
    app, root = settings_app
    path = root / 'config/runtime.yaml'
    path.parent.mkdir(parents=True)
    path.write_text(raw)
    with pytest.raises(ValueError, match='shared YAML alias'):
        patch_integer(raw, ('plugins', 'ide', 'goal', 'default_max_rounds'), 512)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            client.headers['Authorization'] = 'Bearer settings-test'
            initial = await client.get('/v1/plugins/ide/goal/settings')
            assert initial.status_code == 200, initial.text
            response = await client.put('/v1/plugins/ide/goal/settings', json={
                'default_max_rounds': 512, 'expected_revision': initial.json()['revision'],
            })
            assert response.status_code == 422, response.text
            assert path.read_text() == raw
    asyncio.run(run())


def test_unrelated_aliases_do_not_prevent_safe_goal_patch():
    raw = 'other: &value 7\ncopy: *value\nplugins:\n  ide:\n    goal:\n      default_max_rounds: 256\n'
    result = patch_integer(raw, ('plugins', 'ide', 'goal', 'default_max_rounds'), 512)
    assert result == raw.replace('default_max_rounds: 256', 'default_max_rounds: 512')
