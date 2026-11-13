"""Keep built-in icon names registered, including tool metadata and attachment helpers.

Run: python -m pytest tests/test_frontend_icon_registry.py
Dynamic plugin names intentionally use the shared SVG fallback.
"""
from pathlib import Path
import re

SRC = Path(__file__).resolve().parents[1] / 'adapters/web/frontend/src'


def test_builtin_icon_names_have_svg_registrations():
    icon = (SRC / 'components/common/Icon.tsx').read_text()
    table = icon.split('const ICON_MAP:', 1)[1].split('interface IconProps', 1)[0]
    registered = set(re.findall(r'^\s+(\w+):', table, re.MULTILINE))
    references = []
    for path in sorted(SRC.rglob('*')):
        if path.suffix not in {'.ts', '.tsx'}:
            continue
        text = path.read_text()
        for match in re.finditer(r'<Icon\b[^>]*?\bname=(?:"([^"]+)"|\{([^}]+)\})', text):
            names = [match[1]] if match[1] else re.findall(r'''['"]([a-z_]+)['"]''', match[2])
            references.extend((name, path.relative_to(SRC)) for name in names)
        references.extend((name, path.relative_to(SRC)) for name in re.findall(r'''\bicon:\s*['"]([a-z_]+)['"]''', text))
        # Local icon-selection helpers and variables are not visible at the JSX call site.
        for expr in re.findall(r'\bconst iconName\s*=([^;]+);', text):
            expr = re.sub(r'''[!=]={1,2}\s*['"][^'"]+['"]''', '', expr)
            references.extend((name, path.relative_to(SRC)) for name in re.findall(r'''['"]([a-z_]+)['"]''', expr))
    attachment = (SRC / 'components/chat/AttachmentList.tsx').read_text()
    helper = attachment.split('function getFileIcon(', 1)[1].split('\n}', 1)[0]
    references.extend((name, 'getFileIcon') for name in re.findall(r'''return ['"]([a-z_]+)['"]''', helper))
    missing = [(name, str(path)) for name, path in references if name not in registered]
    assert references
    assert not missing, f'Unregistered built-in icons: {missing}'


def test_unknown_icons_do_not_render_internal_names_as_text():
    text = (SRC / 'components/common/Icon.tsx').read_text()
    render = text.split('export const Icon =', 1)[1]
    assert 'ICON_MAP[name] || HelpW400' in render
    assert '<span' not in render
    assert '<SvgComponent' in render
