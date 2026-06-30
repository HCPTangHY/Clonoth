# Skill 系统

本文说明 Clonoth 的 Skill 目录结构、`SKILL.md` frontmatter 格式、激活策略和创建方法。

## 目录结构

每个 Skill 是 `skills/` 下的一个目录，固定入口文件为 `SKILL.md`：

```text
skills/
  <name>/
    SKILL.md
```

当前仓库中已有以下 Skill 目录：

- `daily-chat-summary/`
- `discord_emojis/`
- `jina-reader/`
- `qimen-dunjia/`
- `research-archive/`
- `vertex-model-probe/`

Skill 名称必须匹配：

```text
^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$
```

## SKILL.md frontmatter 格式

`SKILL.md` 使用 YAML frontmatter。frontmatter 位于文件开头的两个 `---` 之间，后面是正文。正文会在 Skill 被注入时提供给模型。

示例：

```markdown
---
name: jina-reader
description: 使用 Jina Reader 抓取网页内容
enabled: true
strategy: constant
priority: 50
order: 0
keywords:
  - 网页抓取
  - /https?:\/\//i
scan_depth: 0
---

# Jina Reader

这里写具体用法、限制和工作流程。
```

常用字段如下：

| 字段 | 说明 |
| --- | --- |
| `name` | Skill 名称。缺省时使用目录名。 |
| `description` | 简短说明。未激活时会出现在 Skill 索引中。 |
| `enabled` | 是否启用。缺省为 `true`。 |
| `strategy` | 激活策略。可选 `constant` 或 `normal`，缺省为 `normal`。 |
| `keywords` | 触发关键词。支持普通子串和 `/pattern/flags` 正则写法。 |
| `order` | 注入顺序。同一块内按 `order` 升序排列。 |
| `priority` | 预算不足时的保留优先级。数值越高越优先保留。 |
| `scan_depth` | 关键词匹配时额外扫描最近多少轮对话。`0` 表示只扫描当前用户消息。 |
| `node_ids` | 可选。限制该 Skill 只对指定节点可见。 |

正则关键词支持 `i`、`s`、`m` 标志。普通关键词按小写子串匹配。

## 激活策略

Skill 注入由 `toolbox/skills_runtime.py` 完成。系统会扫描 `skills/*/SKILL.md`，读取 frontmatter 和正文，并按节点权限、启用状态和激活策略决定是否注入。

### constant

`strategy: constant` 表示 Skill 始终注入。它会进入稳定的 system 消息块：

```text
[SKILLS:CONSTANT]
...
[/SKILLS:CONSTANT]
```

这种方式适合通用规则、固定工具用法、回复风格或经常需要的背景知识。

### normal + keyword matching

`strategy: normal` 表示按关键词激活。系统会扫描当前用户指令；如果 `scan_depth` 大于 0，还会追加最近若干轮用户和助手消息一起匹配。

匹配成功后，Skill 正文会进入动态 system 消息块：

```text
[SKILLS:ACTIVE]
...
[/SKILLS:ACTIVE]
```

没有关键词，或本轮没有匹配的 normal Skill，不会直接注入正文。它会出现在索引中：

```text
[SKILLS:INDEX]
以下 skill 未被激活。如果当前任务需要，可通过 read_file 读取对应 path 的全文。
- name: example
  description: ...
  path: skills/example/SKILL.md
[/SKILLS:INDEX]
```

这样可以减少上下文占用，同时让模型知道有可读取的 Skill。

### 节点访问控制

节点配置中的 `skills` 字段可以控制 Skill 可见性：

```yaml
skills:
  mode: all
```

支持的模式：

- `all`：允许读取所有启用的 Skill。
- `allowlist`：只允许 `allow` 列表中的 Skill。
- `none`：不注入 Skill。

Skill 自身也可以通过 `node_ids` 限制可见节点。两层过滤都会生效。

### 预算控制

`config/runtime.yaml` 中的 `skills.max_budget_chars` 控制 Skill 正文最大注入字符数。`0` 表示不限制。

如果启用预算限制，系统会按 `priority` 从高到低保留 constant 和 active Skill。超出预算的 Skill 会退回索引，不直接注入正文。最终注入顺序仍由 `order` 控制。

## 创建新 Skill 的方法

可以使用两种方式创建 Skill。

### 方式一：使用工具创建

推荐使用 `create_or_update_skill`。它会创建或更新：

```text
skills/<name>/SKILL.md
```

示例参数：

```yaml
name: web-reader
description: 当用户要求读取网页时，说明抓取网页的工作流程
enabled: true
strategy: normal
keywords:
  - 网页
  - 链接
  - /https?:\/\//i
priority: 10
order: 0
scan_depth: 0
content: |
  ---
  name: web-reader
  description: 当用户要求读取网页时，说明抓取网页的工作流程
  enabled: true
  strategy: normal
  keywords:
    - 网页
    - 链接
  ---

  # Web Reader

  当用户提供网页链接时，先确认目标，再读取网页内容并总结。
```

如果传入 `content`，工具会解析并规范化 frontmatter。`name`、`description`、`enabled`、`strategy`、`keywords`、`order`、`priority`、`scan_depth` 等参数会覆盖或补充 frontmatter。

### 方式二：手工创建文件

也可以手工创建目录和文件：

```text
skills/my-skill/SKILL.md
```

手工创建时建议包含完整 frontmatter，并在正文中写清楚：

- Skill 适用于什么任务。
- 何时必须使用。
- 使用步骤。
- 注意事项和限制。
- 如需调用工具，应写明工具名和参数要求。

创建 normal Skill 时，应尽量给出明确关键词。没有关键词的 normal Skill 默认只出现在索引中，不会自动注入正文。