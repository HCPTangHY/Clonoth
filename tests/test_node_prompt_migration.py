"""Offline node-file and prompt-rendering migration checks.

Run: PYTHONDONTWRITEBYTECODE=1 python3.11 -B -m unittest discover -s tests \
    -p 'test_node_prompt_migration.py' -v
Temporary workspaces are always created under this repository's data/temp/.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.faces import nodes as nodes_face
from engine.node import Node, _node_from_dict, load_node, node_file_candidates
from engine.prompt import assemble_prompt
from engine.registry_core import DisposalLedger


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / 'data/temp'
        temp_root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='node-prompt-', dir=temp_root)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write_node(self, relative_path, name):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'type: ai\nname: {name}\ntool_mode: native\noutput_mode: hybrid\n',
            encoding='utf-8',
        )
        return path

    def parse_node(self, **data):
        return _node_from_dict(self.root, 'example', {
            'tool_mode': 'native', 'output_mode': 'hybrid', **data,
        })


class NodeFileTests(WorkspaceTests):
    def test_candidate_order_is_shared_and_has_no_legacy_directory(self):
        self.assertEqual(node_file_candidates(self.root, 'example'), (
            self.root / 'config/nodes/example.local.yaml',
            self.root / 'config/nodes/example.yaml',
            self.root / 'engine/builtin/nodes/example.yaml',
        ))

    def test_local_user_builtin_priority_and_override_removal(self):
        self.write_node('engine/builtin/nodes/example.yaml', 'builtin')
        user = self.write_node('config/nodes/example.yaml', 'user')
        local = self.write_node('config/nodes/example.local.yaml', 'local')
        self.assertEqual(load_node(self.root, 'example').name, 'local')
        local.unlink()
        self.assertEqual(load_node(self.root, 'example').name, 'user')
        user.unlink()
        self.assertEqual(load_node(self.root, 'example').name, 'builtin')

    def test_plugin_declaration_still_overrides_local_files(self):
        self.write_node('config/nodes/example.local.yaml', 'local')
        declaration = {
            'type': 'ai', 'name': 'plugin', 'tool_mode': 'native', 'output_mode': 'hybrid',
        }
        with patch('engine.node.declared_node', return_value=declaration):
            self.assertEqual(load_node(self.root, 'example').name, 'plugin')
        self.assertEqual(load_node(self.root, 'example').name, 'local')

    def test_builtin_file_loads_without_a_registered_plugin(self):
        self.write_node('engine/builtin/nodes/system.example.yaml', 'builtin-system')
        with patch('engine.node.declared_node', return_value=None):
            node = load_node(self.root, 'system.example')
        self.assertEqual(node.id, 'system.example')
        self.assertEqual(node.name, 'builtin-system')

    def test_legacy_system_directory_is_not_a_fallback(self):
        self.write_node('engine/system_nodes/example.yaml', 'retired')
        self.assertIsNone(load_node(self.root, 'example'))
        self.write_node('config/nodes/example.yaml', 'current')
        self.assertEqual(load_node(self.root, 'example').name, 'current')

    def test_local_file_changes_are_visible_after_mtime_changes(self):
        local = self.write_node('config/nodes/example.local.yaml', 'first')
        self.assertEqual(load_node(self.root, 'example').name, 'first')
        before = local.stat()
        self.write_node('config/nodes/example.local.yaml', 'second')
        os.utime(local, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        self.assertNotEqual(local.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(load_node(self.root, 'example').name, 'second')

    def test_invalid_access_lists_are_still_rejected(self):
        for field in ('allow', 'deny'):
            for value in ('read_file', {'read_file': ['read']}, ('read_file',)):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, 'must be lists'):
                    self.parse_node(tool_access={'mode': 'all', field: value})

    def test_permission_rules_control_names_and_constant_memory_are_preserved(self):
        names = [
            'finish', 'ask', 'intermediate_reply', 'compact_context', 'dispatch_to',
            'spawn_worker', 'manage_workers', 'switch_node',
        ]
        node = self.parse_node(
            control_tools=names + ['not-a-control-tool', 'preempt_task', 'cancel_active_tasks'],
            memories={'mode': 'constant'},
            tool_access={'mode': 'allowlist', 'allow': [{'manage_memory': ['list', 'upsert']}],
                         'deny': ['manage_memory:delete']},
        )
        self.assertEqual(node.control_tools, names)
        self.assertEqual(node.memory_access.mode, 'constant')
        self.assertEqual(node.tool_access.allow, ['manage_memory:list,upsert'])
        self.assertEqual(node.tool_access.deny, ['manage_memory:delete'])

    def test_retired_worker_controls_do_not_authorize_their_replacement(self):
        for retired in ('preempt_task', 'cancel_active_tasks'):
            with self.subTest(retired=retired):
                node = self.parse_node(control_tools=[retired])
                self.assertEqual(node.control_tools, [])

    def test_explicit_unknown_provider_is_not_silently_inherited(self):
        for name in ('node-prompt-test-unknown-provider', 'gemini'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Unknown provider'):
                self.parse_node(provider=name)
        self.assertEqual(self.parse_node(provider='').provider, '')


class DeclaredNodeLifecycleTests(WorkspaceTests):
    """Declared-node ownership must survive data hot reloads.

    Regression: register() captured the declaration dict and proved ownership
    with ``current['data'] is data``. declared_node() always replaces the
    payload on the first read (no cached mtime) and on every mtime change, so
    the captured dict stopped matching and unloading a plugin left its node
    registered forever.
    """

    def setUp(self):
        super().setUp()
        self.ledger = DisposalLedger()
        self.face = nodes_face.PluginNodesFace()
        self.face.set_disposal_ledger(self.ledger)
        # The declaration store is module-global by design (load_node reads it
        # without a context); keep each test isolated without rebinding it.
        self.addCleanup(nodes_face._DECLS.clear)

    def declare(self, plugin, decl, source_path=''):
        with self.ledger.collecting(plugin):
            return self.face.register(decl, source_path=source_path)

    def inline(self, name):
        return {'id': 'example', 'type': 'ai', 'name': name,
                'tool_mode': 'native', 'output_mode': 'hybrid'}

    def write_declaration(self, name, node_id='example'):
        path = self.root / 'plugins/example/nodes' / f'{node_id}.yaml'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'type: ai\nname: {name}\ntool_mode: native\noutput_mode: hybrid\n',
                        encoding='utf-8')
        return path

    def touch_forward(self, path):
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    def test_unload_after_first_file_read_removes_the_declaration(self):
        self.write_node('config/nodes/example.yaml', 'file-fallback')
        source = self.write_declaration('plugin-v1')
        self.declare('plugin_a', self.inline('inline-v1'), source_path=str(source))
        # First read has no cached mtime, so the payload is replaced by the file.
        self.assertEqual(load_node(self.root, 'example').name, 'plugin-v1')
        self.assertEqual(self.ledger.unload('plugin_a'), {
            'plugin': 'plugin_a', 'disposed': 1, 'errors': [],
        })
        self.assertIsNone(nodes_face.declared_node('example'))
        self.assertEqual(nodes_face.declared_node_owner('example'), '')
        self.assertEqual(load_node(self.root, 'example').name, 'file-fallback')

    def test_unload_after_mtime_reload_removes_the_declaration(self):
        source = self.write_declaration('plugin-v1')
        self.declare('plugin_a', self.inline('inline-v1'), source_path=str(source))
        self.assertEqual(nodes_face.declared_node('example')['name'], 'plugin-v1')
        self.write_declaration('plugin-v2')
        self.touch_forward(source)
        self.assertEqual(nodes_face.declared_node('example')['name'], 'plugin-v2')
        # Hot reload keeps the registration identity, so attribution survives.
        self.assertEqual(nodes_face.declared_node_owner('example'), 'plugin_a')
        self.assertEqual(self.ledger.unload('plugin_a')['disposed'], 1)
        self.assertIsNone(nodes_face.declared_node('example'))
        self.assertIsNone(load_node(self.root, 'example'))

    def test_stale_disposer_cannot_delete_a_newer_registration(self):
        old = self.declare('plugin_a', self.inline('v1'))
        new = self.declare('plugin_a', self.inline('v2'))
        old()
        self.assertEqual(nodes_face.declared_node('example')['name'], 'v2')
        new()
        self.assertIsNone(nodes_face.declared_node('example'))

    def test_stale_disposer_survives_a_hot_reload_in_between(self):
        source = self.write_declaration('plugin-v1')
        old = self.declare('plugin_a', self.inline('inline-v1'), source_path=str(source))
        self.assertEqual(nodes_face.declared_node('example')['name'], 'plugin-v1')
        self.write_declaration('plugin-v2')
        self.touch_forward(source)
        new = self.declare('plugin_a', self.inline('inline-v2'), source_path=str(source))
        # A path-backed declaration still resolves through its file (source of
        # truth), but the registration token is untouched by that reload.
        self.assertEqual(nodes_face.declared_node('example')['name'], 'plugin-v2')
        old()
        self.assertEqual(nodes_face.declared_node('example')['name'], 'plugin-v2')
        new()
        self.assertIsNone(nodes_face.declared_node('example'))

    def test_same_owner_reregistration_is_idempotent(self):
        decl = self.inline('stable')
        disposer = self.declare('plugin_a', decl)
        version = nodes_face.registry_version()
        duplicate = self.declare('plugin_a', decl)
        self.assertEqual(nodes_face.registry_version(), version)
        self.assertEqual(self.ledger.counts(), {'plugin_a': 1})
        duplicate()
        self.assertEqual(nodes_face.declared_node('example')['name'], 'stable')
        # The original disposer stays authoritative, so unload still works.
        self.assertEqual(self.ledger.unload('plugin_a')['disposed'], 1)
        self.assertIsNone(nodes_face.declared_node('example'))
        disposer()
        self.assertIsNone(nodes_face.declared_node('example'))

    def test_path_backed_reregistration_by_the_same_owner_is_idempotent(self):
        # The builtin loader re-registers its file-backed declarations on every
        # ai_step; an unchanged file must not invalidate the runner caches.
        source = self.write_declaration('plugin')
        decl = {'id': 'example', 'type': 'ai', 'name': 'plugin',
                'tool_mode': 'native', 'output_mode': 'hybrid'}
        self.declare('plugin_a', dict(decl), source_path=str(source))
        version = nodes_face.registry_version()
        self.declare('plugin_a', dict(decl), source_path=str(source))
        self.assertEqual(nodes_face.registry_version(), version)
        self.assertEqual(self.ledger.counts(), {'plugin_a': 1})
        self.assertEqual(nodes_face.declared_node('example')['name'], 'plugin')
        self.assertEqual(self.ledger.unload('plugin_a')['disposed'], 1)
        self.assertIsNone(nodes_face.declared_node('example'))

    def test_version_bumps_once_per_effective_registration(self):
        first = self.declare('plugin_a', self.inline('v1'))
        self.assertGreater(nodes_face.registry_version(), 0)
        self.declare('plugin_a', self.inline('v2'))
        before = nodes_face.registry_version()
        first()
        self.assertEqual(nodes_face.registry_version(), before)
        self.ledger.unload('plugin_a')
        self.assertEqual(nodes_face.registry_version(), before + 1)

    def test_other_plugins_cannot_claim_a_live_declaration(self):
        self.declare('plugin_a', self.inline('owned'))
        with self.assertRaisesRegex(ValueError, "already declared by plugin 'plugin_a'"):
            self.declare('plugin_b', self.inline('hijack'))
        self.assertEqual(nodes_face.declared_node('example')['name'], 'owned')
        self.ledger.unload('plugin_a')
        self.declare('plugin_b', self.inline('handover'))
        self.assertEqual(nodes_face.declared_node_owner('example'), 'plugin_b')
        self.assertEqual(nodes_face.declared_node('example')['name'], 'handover')

    def test_declaration_precedence_over_file_sources_is_unchanged(self):
        self.write_node('config/nodes/example.local.yaml', 'local')
        source = self.write_declaration('plugin')
        self.declare('plugin_a', {'id': 'example', 'type': 'ai'}, source_path=str(source))
        self.assertEqual(load_node(self.root, 'example').name, 'plugin')
        self.assertEqual(nodes_face.declared_node_path('example'), str(source))
        self.ledger.unload('plugin_a')
        self.assertEqual(load_node(self.root, 'example').name, 'local')
        self.assertEqual(nodes_face.declared_node_path('example'), '')


class PromptVariablesTests(WorkspaceTests):
    def render(self, template, variables):
        node = Node(id='example', type='ai', name='Example', prompt=template)
        return assemble_prompt(self.root, node, variables=variables)

    def test_static_defaults_are_isolated_from_dynamic_variables_and_time(self):
        fields = '{{node_name}}|{{workspace_root}}|{{now}}|{{instruction}}|{{custom}}'
        variables = {
            'node_name': 'override', 'workspace_root': 'override-root',
            'now': 'test-time', 'instruction': 'request', 'custom': 'custom-value',
        }
        messages = self.render(fields + '\n# %%DYNAMIC%%\n' + fields, variables)
        self.assertEqual(messages, [
            {'role': 'system', 'content': f'Example|{self.root.resolve()}|{{{{now}}}}|{{{{instruction}}}}|{{{{custom}}}}'},
            {'role': 'system', 'content': 'override|override-root|test-time|request|custom-value'},
        ])
        self.assertEqual(variables['node_name'], 'override')
        self.assertEqual(variables['now'], 'test-time')

    def test_static_prefix_stays_equal_across_per_turn_values(self):
        template = '{{node_name}} {{now}} {{instruction}}\n# %%DYNAMIC%%\n{{now}} {{instruction}}'
        first = self.render(template, {'now': 'first-time', 'instruction': 'first-request'})
        second = self.render(template, {'now': 'next-time', 'instruction': 'next-request'})
        self.assertEqual(first[0], second[0])
        self.assertNotEqual(first[1], second[1])

    def test_no_marker_still_uses_the_complete_variable_set(self):
        self.assertEqual(self.render('{{node_name}} {{now}} {{instruction}}', {
            'node_name': 'override', 'now': 'time', 'instruction': 'request',
        }), [{'role': 'system', 'content': 'override time request'}])

    def test_block_prompt_still_uses_complete_variables(self):
        result = self.render([
            {'role': 'system', 'content': '{{node_name}} {{now}} {{instruction}}'},
            {'role': 'history'},
            {'role': 'user', 'content': '{{custom}}', 'depth': 2},
        ], {'node_name': 'override', 'now': 'time', 'instruction': r'中文\x', 'custom': 'value'})
        self.assertEqual(result, [
            {'role': 'system', 'content': r'override time 中文\x'},
            {'role': 'history'},
            {'role': 'user', 'content': 'value', 'depth': 2},
        ])

    def test_user_backslash_sequences_are_not_decoded(self):
        instruction = r'C:\users\new\x \u1234 \n'
        result = self.render('Static\\n# %%DYNAMIC%%\\n{{instruction}}', {'instruction': instruction})
        self.assertEqual(result, [{'role': 'system', 'content': 'Static'},
                                  {'role': 'system', 'content': instruction}])

    def test_invalid_template_escape_does_not_crash(self):
        result = self.render(r'Broken \x {{instruction}}', {'instruction': 'text'})
        self.assertEqual(result, [{'role': 'system', 'content': r'Broken \x text'}])

    def test_variable_text_cannot_add_template_boundaries(self):
        instruction = '# %%DYNAMIC%% injected'
        self.assertEqual(self.render('Static {{instruction}}', {'instruction': instruction}), [
            {'role': 'system', 'content': 'Static ' + instruction},
        ])
        divided = self.render('Static\n# %%DYNAMIC%%\n{{instruction}}', {'instruction': instruction})
        self.assertEqual(len(divided), 2)
        self.assertEqual(divided[1]['content'], instruction)

    def test_include_expansion_and_containment_are_preserved(self):
        nodes = self.root / 'config/nodes'
        nodes.mkdir(parents=True)
        (nodes / 'included.txt').write_text('Header {{node_name}}', encoding='utf-8')
        (self.root / 'outside.txt').write_text('outside-fixture', encoding='utf-8')
        result = self.render(
            '{{include:included.txt}} {{include:../../outside.txt}}\n# %%DYNAMIC%%\n{{instruction}}',
            {'instruction': 'request'},
        )
        self.assertEqual(result[0]['content'], 'Header Example {{include:../../outside.txt}}')
        self.assertEqual(result[1]['content'], 'request')
        self.assertNotIn('outside-fixture', str(result))


if __name__ == '__main__':
    unittest.main()
