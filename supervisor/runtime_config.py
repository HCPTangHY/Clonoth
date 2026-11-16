"""Serialized atomic runtime writes and comment-preserving scalar patches."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import tempfile
import threading

import yaml

WRITE_LOCK = threading.RLock()


def read_raw(root: Path) -> str:
    try:
        return (root / 'config/runtime.yaml').read_text(encoding='utf-8')
    except FileNotFoundError:
        return ''


def revision(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def write_raw(root: Path, raw: str) -> None:
    with WRITE_LOCK:
        path = root / 'config/runtime.yaml'
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = root / 'data/temp'
        temp.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=temp, prefix='runtime-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                try:
                    os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
                except FileNotFoundError:
                    pass
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)


def patch_integer(raw: str, keys: tuple[str, ...], value: int) -> str:
    """Use YAML source marks, not a full dump, so unrelated text stays intact."""
    doc = yaml.safe_load(raw)
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise ValueError('runtime must be a mapping')
    expected = copy.deepcopy(doc)
    current = expected
    for key in keys[:-1]:
        current = current.setdefault(key, {})
        if not isinstance(current, dict):
            raise ValueError('configuration parent must be a mapping')
    current[keys[-1]] = value
    node = yaml.compose(raw)
    # Alias marks point at the anchor: replacing them could change unrelated settings.
    references: dict[int, int] = {}
    pending = [node] if node is not None else []
    while pending:
        item = pending.pop()
        identity = id(item)
        references[identity] = references.get(identity, 0) + 1
        if references[identity] > 1:
            continue
        if isinstance(item, yaml.MappingNode):
            pending.extend(child for pair in item.value for child in pair)
        elif isinstance(item, yaml.SequenceNode):
            pending.extend(item.value)
    if node is None:
        result = raw + ('\n' if raw and not raw.endswith('\n') else '') + yaml.safe_dump(expected, sort_keys=False)
    else:
        for depth, key in enumerate(keys):
            if not isinstance(node, yaml.MappingNode):
                raise ValueError('configuration parent must be a mapping')
            matches = [(k, v) for k, v in node.value if k.value == key]
            if len(matches) > 1:
                raise ValueError('duplicate configuration key')
            if matches:
                child = matches[0][1]
                if references.get(id(child), 0) > 1:
                    raise ValueError('cannot patch a shared YAML alias; use the raw configuration editor')
                if depth == len(keys) - 1:
                    if not isinstance(child, yaml.ScalarNode):
                        raise ValueError('configuration value must be a scalar')
                    result = raw[:child.start_mark.index] + str(value) + raw[child.end_mark.index:]
                    break
                node = child
                continue
            subtree = value
            for name in reversed(keys[depth:]):
                subtree = {name: subtree}
            if node.flow_style:
                offset = node.end_mark.index - 1
                text = yaml.safe_dump(subtree, default_flow_style=True, sort_keys=False).strip()[1:-1]
                result = raw[:offset] + (', ' if node.value else '') + text + raw[offset:]
            else:
                offset = raw.rfind('\n', 0, node.end_mark.index) + 1
                if node.end_mark.index == len(raw):
                    offset = len(raw)
                indent = node.value[0][0].start_mark.column if node.value else depth * 2
                text = yaml.safe_dump(subtree, sort_keys=False)
                text = ''.join(' ' * indent + line if line.strip() else line for line in text.splitlines(keepends=True))
                prefix = '\n' if offset and raw[offset - 1] != '\n' else ''
                result = raw[:offset] + prefix + text + raw[offset:]
            break
    if yaml.safe_load(result) != expected:
        raise ValueError('cannot safely patch this YAML layout')
    return result
