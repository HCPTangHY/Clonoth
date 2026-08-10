# Skill 系统

本文说明 Clonoth 的 Skill 目录结构、`SKILL.md` frontmatter 格式、激活策略和创建方法。

## 目录结构

每个 Skill 是 `skills/` 下的一个目录，固定入口文件为 `SKILL.md`：

```text
skills/
  <name>/
    SKILL.md
```

Skill 名称必须匹配：

```text
^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$
```

## SKILL.md frontmatter 格式

```yaml
---
strategy: normal       # constant | normal
keywords:              # normal 策略下触发注入的关键词列表
  - keyword1
  - keyword2
enabled: true          # 是否启用
description: ...       # 描述
priority: 0            # 数值越高，constant 注入越靠前
scan_depth: 5          # normal 策略下扫描最近几轮消息
---

# 正文内容

Skill 正文以 Markdown 格式编写，会被原样注入到模型的 system prompt 中。
```

## 激活策略

### constant

始终注入到节点上下文中。适合全局规则、输出格式约定、角色设定等。

注入内容包裹在 `[SKILLS:CONSTANT]` / `[/SKILLS:CONSTANT]` 标记中。

### normal

只在最近消息中匹配到 `keywords` 时才注入。适合领域知识、工具使用指南等按需激活的内容。

`scan_depth` 控制回看范围，默认 5 轮。匹配的 Skill 包裹在 `[SKILLS:ACTIVE]` / `[/SKILLS:ACTIVE]` 标记中。

未被激活的 normal Skill 会生成一份摘要索引，包裹在 `[SKILLS:INDEX]` / `[/SKILLS:INDEX]` 标记中，提示模型可以通过 `read_file` 读取完整内容。

## 节点级权限

节点 YAML 中的 `skills` 字段控制该节点可以使用哪些 Skill：

```yaml
skills:
  mode: all      # all | none | allowlist | deny
  list:          # allowlist / deny 模式下的 Skill 名称列表
    - skill-name
```

- `all`：允许使用所有启用的 Skill。
- `none`：不注入 Skill。
- `allowlist`：只注入 `list` 中指定的 Skill。
- `deny`：注入除 `list` 以外的所有 Skill。

## 创建与管理

### 通过 AI 工具

```
create_or_update_skill(name="my-skill", description="...", content="...")
list_skills()
delete_skill(name="my-skill")
```

`create_or_update_skill` 会自动规范化 frontmatter。`delete_skill` 受审批保护。

### 手动创建

在 `skills/` 下新建目录，写入 `SKILL.md`。frontmatter 和正文格式参见上文。
