/* [AutoC 2026-08-24] IDE plugin — workspace file preview slot module.
 * Why: the workspace file tree's preview region is a host-reserved slot; the
 * rendering of file content is plugin business. How: on mount/update, fetch
 * the file through ctx.api.request (session file endpoint, auth injected by
 * the host), then render by content shape — text into a numbered <pre>,
 * images into an <img> via object URL, anything else as a binary notice.
 * Purpose: preview any workspace file without host code knowing file types.
 *
 * Contract: ctx.data = { sessionId, path } — path is workspace-relative as
 * returned by the tree API. api.request parses by content type: JSON →
 * object, text/* → string, everything else → Blob.
 */

const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'ico', 'bmp', 'avif']);
const MAX_TEXT_BYTES = 512 * 1024;

function extOf(path) {
  const name = String(path).split('/').pop() || '';
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
}

export default {
  mount(ctx) {
    const root = document.createElement('div');
    root.className = 'ide-preview';
    ctx.el.appendChild(root);
    this._root = root;
    this._ctx = ctx;
    this._path = null;
    this._seq = 0;
    this._blobUrl = null;
    this._load(ctx);
  },

  update(ctx) {
    this._ctx = ctx;
    const path = ctx.data && ctx.data.path;
    if (path !== this._path) this._load(ctx);
  },

  destroy() {
    this._releaseBlob();
    this._root = null;
    this._ctx = null;
  },

  _releaseBlob() {
    if (this._blobUrl) {
      URL.revokeObjectURL(this._blobUrl);
      this._blobUrl = null;
    }
  },

  _status(text) {
    this._releaseBlob();
    this._root.innerHTML = '';
    const div = document.createElement('div');
    div.className = 'ide-preview-status';
    div.textContent = text;
    this._root.appendChild(div);
  },

  async _load(ctx) {
    const path = ctx.data && ctx.data.path;
    const sessionId = ctx.data && ctx.data.sessionId;
    this._path = path || null;
    const seq = ++this._seq;
    if (!path || !sessionId) {
      this._status('');
      return;
    }
    if (!ctx.api || !ctx.api.request) {
      this._status('预览不可用：缺少 api.request');
      return;
    }
    this._status('加载中…');
    let body;
    try {
      body = await ctx.api.request(
        '/sessions/' + encodeURIComponent(sessionId) + '/file?path=' + encodeURIComponent(path),
      );
    } catch (err) {
      if (seq !== this._seq) return; // a newer load superseded this one
      this._status('加载失败：' + (err && err.message ? err.message : String(err)));
      return;
    }
    if (seq !== this._seq) return;

    const ext = extOf(path);
    if (body instanceof Blob) {
      if (IMAGE_EXTS.has(ext)) {
        this._releaseBlob();
        this._blobUrl = URL.createObjectURL(body);
        this._root.innerHTML = '';
        const img = document.createElement('img');
        img.className = 'ide-preview-image';
        img.src = this._blobUrl;
        img.alt = path;
        this._root.appendChild(img);
        return;
      }
      // Unknown binary type — try reading as text, else report size.
      const text = await body.text().catch(() => null);
      if (text == null || text.includes('')) {
        this._status('二进制文件（' + body.size + ' B），不支持预览');
        return;
      }
      this._renderText(text);
      return;
    }
    if (typeof body === 'string') {
      this._renderText(body);
      return;
    }
    // JSON-shaped file content (endpoint returned parsed JSON).
    this._renderText(JSON.stringify(body, null, 2));
  },

  _renderText(text) {
    this._releaseBlob();
    this._root.innerHTML = '';
    let truncated = false;
    if (text.length > MAX_TEXT_BYTES) {
      text = text.slice(0, MAX_TEXT_BYTES);
      truncated = true;
    }
    const pre = document.createElement('pre');
    pre.className = 'ide-preview-code';
    const lines = text.split('\n');
    // Line numbers as a per-line span column so copy-paste excludes them.
    const frag = document.createDocumentFragment();
    lines.forEach((line, i) => {
      const row = document.createElement('div');
      row.className = 'ide-preview-line';
      const no = document.createElement('span');
      no.className = 'ide-preview-lineno';
      no.textContent = String(i + 1);
      const code = document.createElement('span');
      code.className = 'ide-preview-linetext';
      code.textContent = line;
      row.appendChild(no);
      row.appendChild(code);
      frag.appendChild(row);
    });
    pre.appendChild(frag);
    this._root.appendChild(pre);
    if (truncated) {
      const note = document.createElement('div');
      note.className = 'ide-preview-status';
      note.textContent = '文件过大，仅显示前 ' + MAX_TEXT_BYTES + ' 字节';
      this._root.appendChild(note);
    }
  },
};
