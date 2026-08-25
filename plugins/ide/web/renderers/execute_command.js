// execute_command 工具卡内容渲染器。
// 槽位：tool_card_content:execute_command。data 是宿主给的工具快照
// { toolName, arguments, result, error, status, elapsedMs, nodeId }。
//
// 命令保持单个大代码块，按行对指令首词做类别着色（纯视觉变换，不切分
// 不重组，引号/heredoc 内容原样保留在原行）。超时以标签显示在块头。

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
}

const CMD_CLASSES = [
  /^(cd|ls|pwd|tree|pushd|popd)\b/,                                                        // 导航
  /^(cat|head|tail|less|more|grep|rg|find|sed|awk|wc|diff|sort|uniq|which|file|stat|du|df)\b/, // 检索
  /^(git)\b/,                                                                               // VCS
  /^(python3?|node|npx|npm|pnpm|yarn|bun|deno|ruby|go|cargo|java|bash|sh|zsh|tsc|esbuild)\b/, // 运行时
  /^(curl|wget|ping|ssh|scp|rsync|nc)\b/,                                                   // 网络
  /^(cp|mv|mkdir|touch|rm|rmdir|ln|chmod|chown|tar|zip|unzip|install)\b/,                   // 写操作
  /^(make|cmake|docker|docker-compose|systemctl|service|pm2)\b/,                            // 构建
];
const CMD_COLORS = ['#4078f2', '#50a14f', '#986801', '#e45649', '#0184bc', '#c18401', '#a626a4'];

function lineColor(line) {
  const first = (line.trim().match(/^[^\s]+/) || [''])[0];
  for (let i = 0; i < CMD_CLASSES.length; i++) {
    if (CMD_CLASSES[i].test(first)) return CMD_COLORS[i];
  }
  return null;
}

// 逐行处理：行首词与行内段首词（&&/||/;/| 之后的第一个 token）命中类别
// 则用 span 着色，其余原样。不切行、不识别引号与 heredoc——着色是纯视觉
// 标记，误着色的最坏结果是内嵌脚本的某个词带颜色，不影响结构与内容。
function renderCommandText(cmd) {
  return cmd.split('\n').map((line) => {
    let out = '';
    let last = 0;
    // 段首词位置：行首 或 &&/||/;/| 之后
    const re = /(^|&&|\|\||[;|])(\s*)(\S+)/g;
    let m;
    while ((m = re.exec(line)) !== null) {
      const word = m[3];
      const color = lineColor(word);
      if (!color) continue;
      out += esc(line.slice(last, m.index)) + esc(m[1]) + esc(m[2]) +
        '<span style="color:' + color + ';font-weight:600">' + esc(word) + '</span>';
      last = m.index + m[0].length;
    }
    out += esc(line.slice(last));
    return out;
  }).join('\n');
}

function kv(label, value) {
  if (value === undefined || value === null || value === '') return '';
  return '<div class="ide-tc-kv"><span class="ide-tc-k">' + esc(label) + '</span>' +
    '<span class="ide-tc-v">' + esc(value) + '</span></div>';
}

function render(ctx) {
  const d = ctx.data || {};
  const args = d.arguments || {};
  const parts = [];

  if (args.command) {
    let block = '<div class="ide-tc-cmd"><div class="ide-tc-cmdhead"><span>命令</span>';
    if (args.timeout_sec != null) {
      block += '<span class="ide-tc-timeout">' + esc(args.timeout_sec) + 's</span>';
    }
    block += '</div><pre class="ide-tc-code ide-tc-cmdraw">' +
      renderCommandText(String(args.command)) + '</pre></div>';
    parts.push(block);
  }

  const meta = kv('工作目录', args.workdir || '')
    + kv('节点', d.nodeId || '')
    + kv('耗时', d.elapsedMs != null ? (d.elapsedMs / 1000).toFixed(1) + 's' : '');
  if (meta) parts.push('<div class="ide-tc-block">' + meta + '</div>');

  const out = d.error || d.result || '';
  if (out) {
    const cls = d.error ? 'ide-tc-code ide-tc-err' : 'ide-tc-code';
    parts.push('<div class="ide-tc-block"><div class="ide-tc-label">' +
      (d.error ? '错误' : '输出') + '</div><pre class="' + cls + '">' + esc(out) + '</pre></div>');
  }

  ctx.el.innerHTML = parts.join('');
}

export default {
  mount(ctx) { render(ctx); },
  update(ctx) { render(ctx); },
};
