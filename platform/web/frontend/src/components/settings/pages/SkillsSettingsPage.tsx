// [2026-06-02] Skill management settings page.
// Why: skills are Markdown files with frontmatter that operators frequently adjust.
// How: list parsed skill metadata and support create/delete actions here, while the
// full frontmatter form and Markdown editor live in the Settings right panel. Purpose:
// skill maintenance is split into a clean list and a focused editor.
import { useEffect, useState } from 'react';

import { createSkill, deleteSkill, getSkills, type AdminSkill } from '../../../api/supervisorClient';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button } from '../../common';
import { AuthRequired, Card, FieldLabel, PageHeader, PageShell, StatusText, TextInput } from './settingsPagePrimitives';

function defaultSkillMarkdown(name: string): string {
  // [2026-06-02] Provide a complete starter SKILL.md with frontmatter. Why: the
  // backend create endpoint writes raw content and expects users to supply metadata.
  // How: include enabled, strategy, keywords, order, priority, and scan_depth fields.
  // Purpose: new skills appear correctly and can be refined immediately in the right panel.
  return `---\nname: ${name}\ndescription: 通过设置页面创建的技能。\nenabled: true\nstrategy: normal\nkeywords: []\norder: 0\npriority: 0\nscan_depth: 0\n---\n\n# ${name}\n\n请在这里编写技能内容。\n`;
}

export const SkillsSettingsPage = () => {
  // [2026-06-02] Pull the right-panel opener into the list page. Why: selecting a
  // skill on mobile should reveal the skill editor immediately. How: call the shared
  // settings-store setter from each row click. Purpose: users do not need a second tap
  // on the small header chevron after choosing an item.
  const { adminToken, isAuthenticated, setRightPanelOpen } = useSettingsStore();
  const { selectedSkill, setSelectedSkill } = useSettingsSelectionStore();
  const [skills, setSkills] = useState<AdminSkill[]>([]);
  const [newName, setNewName] = useState('');
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(false);

  const load = async () => {
    if (!adminToken || !isAuthenticated) return;
    setLoading(true);
    try {
      const items = await getSkills(adminToken);
      setSkills(items);
      if (selectedSkill && !items.some((item) => item.name === selectedSkill.name)) setSelectedSkill(null);
      setMessage('');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '加载技能失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, [adminToken, isAuthenticated]);

  useEffect(() => {
    // [2026-06-02] Refresh parsed skill metadata after the right-panel editor saves.
    // Why: the page no longer owns the raw Markdown state. How: listen for a local
    // browser event emitted by the right panel. Purpose: enabled, strategy, keywords,
    // and description previews update without a full settings reload.
    const handler = () => { void load(); };
    window.addEventListener('settings:skills-updated', handler);
    return () => window.removeEventListener('settings:skills-updated', handler);
  }, [adminToken, isAuthenticated, selectedSkill?.name]);

  const create = async () => {
    if (!adminToken) return;
    const name = newName.trim();
    if (!name) { setMessage('请输入技能名称'); return; }
    try {
      await createSkill(adminToken, { id: name, content: defaultSkillMarkdown(name) });
      setNewName('');
      setMessage('技能已创建，请在右栏编辑 frontmatter 和正文。');
      await load();
      setSelectedSkill({ name, description: '通过设置页面创建的技能。', enabled: true, strategy: 'normal', keywords: [] });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '创建技能失败');
    }
  };

  const remove = async () => {
    if (!adminToken || !selectedSkill) return;
    if (!window.confirm(`确定要删除技能 ${selectedSkill.name} 吗？`)) return;
    try {
      await deleteSkill(adminToken, selectedSkill.name);
      setSelectedSkill(null);
      setMessage('技能已删除');
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '删除技能失败');
    }
  };

  return (
    <PageShell>
      <PageHeader description="管理 skills 目录下的 SKILL.md 文件。选择技能后，请在右栏编辑 frontmatter 和 Markdown 正文。" title="技能管理" />
      {!isAuthenticated ? <AuthRequired /> : (
        <Card title="技能列表" description="列表展示技能名称、启用状态、策略和关键词预览。编辑器位于右栏。">
          <div className="mb-3 flex flex-wrap gap-2">
            <Button disabled={loading} onClick={load}>{loading ? '刷新中...' : '刷新技能'}</Button>
            <Button disabled={!selectedSkill} onClick={remove} variant="danger">删除选中技能</Button>
          </div>
          <div className="max-h-[34rem] space-y-2 overflow-y-auto">
            {skills.map((skill) => (
              <button className={`w-full border p-3 text-left ${selectedSkill?.name === skill.name ? 'border-[var(--duties-text)] bg-[var(--duties-bg)]' : 'border-[var(--duties-border)] bg-[var(--duties-bg)]'}`} key={skill.name} onClick={() => { setSelectedSkill(skill); setMessage(''); setRightPanelOpen(true); }} type="button">
                <div className="flex flex-wrap items-center gap-2"><span className="font-mono text-xs font-semibold">{skill.name}</span><span className={`border px-1.5 py-0.5 text-[0.55rem] ${skill.enabled === false ? 'border-red-200 bg-red-50 text-red-700' : 'border-green-200 bg-green-50 text-green-700'}`}>{skill.enabled === false ? '禁用' : '启用'}</span><span className="font-mono text-[0.6rem] text-[var(--duties-tertiary)]">{skill.strategy || 'normal'}</span></div>
                <p className="mt-1 text-xs text-[var(--duties-secondary)]">{skill.description || skill.body_preview || '无描述'}</p>
                {(skill.keywords || []).length > 0 && <p className="mt-1 truncate font-mono text-[0.65rem] text-[var(--duties-tertiary)]">{(skill.keywords || []).join(', ')}</p>}
              </button>
            ))}
          </div>
          <div className="mt-4 border-t border-[var(--duties-border)] pt-3">
            <FieldLabel htmlFor="new-skill-name">创建技能</FieldLabel>
            <TextInput id="new-skill-name" onChange={(event) => setNewName(event.target.value)} placeholder="技能目录名" value={newName} />
            <Button className="mt-2" onClick={create} variant="primary">创建技能</Button>
          </div>
          <StatusText message={message} />
        </Card>
      )}
    </PageShell>
  );
};
