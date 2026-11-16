"""IDE-owned Goal defaults; patch one runtime key without changing saved Goals."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
import yaml

from supervisor.admin_api import verify_admin_token
from supervisor.runtime_config import WRITE_LOCK, patch_integer, read_raw, revision, write_raw


def _rounds(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 1_000_000:
        raise HTTPException(422, '默认最大续轮数须为 1 至 1000000 的整数。')
    return value


def _value(raw: str) -> int:
    try:
        current = yaml.safe_load(raw) if raw else {}
        if current is None:
            current = {}
        for key in ('plugins', 'ide', 'goal'):
            if not isinstance(current, dict):
                raise ValueError('configuration parent must be a mapping')
            current = current.get(key, {})
        if not isinstance(current, dict):
            raise ValueError('goal must be a mapping')
        return _rounds(current.get('default_max_rounds', 256))
    except (ValueError, yaml.YAMLError) as exc:
        raise HTTPException(422, 'Goal 默认配置无效，未覆盖原文件。') from exc


def add_routes(router: APIRouter) -> None:
    def root_for(request: Request) -> Path:
        runtime = getattr(request.app.state.state, 'execution_runtime', None)
        if runtime is None:
            raise HTTPException(503, 'Goal runtime unavailable')
        return Path(runtime.workspace_root)

    @router.get('/goal/settings', dependencies=[Depends(verify_admin_token)])
    def read_settings(request: Request) -> dict:
        with WRITE_LOCK:
            raw = read_raw(root_for(request))
            return {'default_max_rounds': _value(raw), 'revision': revision(raw)}

    @router.put('/goal/settings', dependencies=[Depends(verify_admin_token)])
    async def save_settings(request: Request) -> dict:
        raw_body = bytearray()
        async for chunk in request.stream():
            if len(raw_body) + len(chunk) > 4096:
                raise HTTPException(413, 'Goal settings body too large')
            raw_body.extend(chunk)
        try:
            body = json.loads(raw_body)
        except (ValueError, UnicodeError):
            raise HTTPException(422, 'Invalid JSON') from None
        if not isinstance(body, dict) or set(body) != {'default_max_rounds', 'expected_revision'}:
            raise HTTPException(422, 'Invalid Goal settings fields')
        value = _rounds(body['default_max_rounds'])
        with WRITE_LOCK:
            root = root_for(request)
            raw = read_raw(root)
            if body['expected_revision'] != revision(raw):
                raise HTTPException(409, '运行配置已更新，请重新读取后保存。')
            _value(raw)
            try:
                updated = patch_integer(raw, ('plugins', 'ide', 'goal', 'default_max_rounds'), value)
            except (ValueError, yaml.YAMLError) as exc:
                raise HTTPException(422, '当前 YAML 布局不能安全修改，请在运行配置编辑器中修改。') from exc
            write_raw(root, updated)
            return {'default_max_rounds': value, 'revision': revision(updated)}
