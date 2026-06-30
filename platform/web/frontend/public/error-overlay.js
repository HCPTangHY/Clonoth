// Clonoth Error Overlay — Vite-style runtime error display.
// Captures uncaught errors, unhandled rejections, and console.error calls,
// then renders them in a modal overlay with navigation, copy, and dismiss.
;(function () {
  var errors = [], overlay = null, idx = 0;

  var S = {
    overlay:
      'position:fixed;inset:0;z-index:999999;background:rgba(0,0,0,0.85);' +
      'display:flex;align-items:center;justify-content:center;' +
      'font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;' +
      '-webkit-font-smoothing:antialiased',
    card:
      'background:#1a1a2e;color:#e0e0e0;border:1px solid #333;border-radius:8px;' +
      'max-width:800px;width:92vw;max-height:85vh;display:flex;flex-direction:column;' +
      'box-shadow:0 25px 50px rgba(0,0,0,0.5)',
    header:
      'display:flex;align-items:center;justify-content:space-between;' +
      'padding:12px 16px;border-bottom:1px solid #333;flex-shrink:0',
    title:
      'color:#ff6b6b;font-size:14px;font-weight:700;display:flex;align-items:center;gap:8px',
    badge: 'background:#ff6b6b;color:#fff;font-size:11px;padding:1px 6px;border-radius:10px',
    nav: 'display:flex;gap:6px;align-items:center;color:#888;font-size:12px',
    btn:
      'background:none;border:1px solid #555;color:#ccc;padding:4px 8px;border-radius:4px;' +
      'cursor:pointer;font-size:12px;font-family:inherit',
    body: 'padding:16px;overflow-y:auto;flex:1;min-height:0',
    pre:
      'margin:0;font-size:13px;line-height:1.6;color:#e0e0e0;white-space:pre-wrap;' +
      'word-break:break-all;user-select:text;-webkit-user-select:text',
    footer:
      'display:flex;gap:8px;padding:10px 16px;border-top:1px solid #333;flex-shrink:0',
    copyBtn:
      'background:#2a2a4a;border:1px solid #555;color:#ccc;padding:6px 14px;border-radius:4px;' +
      'cursor:pointer;font-size:12px;font-family:inherit',
    closeBtn:
      'background:#ff6b6b22;border:1px solid #ff6b6b44;color:#ff6b6b;padding:6px 14px;' +
      'border-radius:4px;cursor:pointer;font-size:12px;font-family:inherit',
    ts: 'color:#666;font-size:11px;margin-bottom:6px',
  };

  function render() {
    if (!overlay || !errors.length) return;
    var e = errors[idx] || errors[0];
    var countEl = overlay.querySelector('[data-count]');
    var navEl = overlay.querySelector('[data-nav]');
    var bodyEl = overlay.querySelector('[data-body]');
    if (countEl) countEl.textContent = errors.length;
    if (navEl) navEl.textContent = (idx + 1) + ' / ' + errors.length;
    if (bodyEl) {
      bodyEl.innerHTML = '';
      var ts = document.createElement('div');
      ts.style.cssText = S.ts;
      ts.textContent = e.time;
      bodyEl.appendChild(ts);
      var pre = document.createElement('pre');
      pre.style.cssText = S.pre;
      pre.textContent = e.text;
      bodyEl.appendChild(pre);
    }
  }

  function build() {
    if (overlay) return overlay;
    overlay = document.createElement('div');
    overlay.style.cssText = S.overlay;
    overlay.innerHTML =
      '<div style="' + S.card + '">' +
        '<div style="' + S.header + '">' +
          '<div style="' + S.title + '">⚠ Runtime Error <span data-count style="' + S.badge + '">0</span></div>' +
          '<div style="' + S.nav + '">' +
            '<button data-prev style="' + S.btn + '">◀</button>' +
            '<span data-nav>1/1</span>' +
            '<button data-next style="' + S.btn + '">▶</button>' +
          '</div>' +
        '</div>' +
        '<div data-body style="' + S.body + '"></div>' +
        '<div style="' + S.footer + '">' +
          '<button data-copy style="' + S.copyBtn + '">📋 复制</button>' +
          '<button data-copyall style="' + S.copyBtn + '">📋 复制全部</button>' +
          '<div style="flex:1"></div>' +
          '<button data-close style="' + S.closeBtn + '">✕ 关闭</button>' +
        '</div>' +
      '</div>';

    overlay.querySelector('[data-close]').onclick = function () {
      overlay.style.display = 'none';
    };
    overlay.querySelector('[data-prev]').onclick = function () {
      idx = Math.max(0, idx - 1);
      render();
    };
    overlay.querySelector('[data-next]').onclick = function () {
      idx = Math.min(errors.length - 1, idx + 1);
      render();
    };
    function copyText(text, btn, label) {
      // navigator.clipboard requires secure context; fall back to execCommand
      function done() {
        btn.textContent = '✓ 已复制';
        setTimeout(function () { btn.textContent = label; }, 1500);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(fallback);
      } else {
        fallback();
      }
      function fallback() {
        try {
          var ta = document.createElement('textarea');
          ta.value = text;
          ta.style.cssText = 'position:fixed;left:-9999px';
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
          done();
        } catch (e) {
          btn.textContent = '❌ 复制失败';
          setTimeout(function () { btn.textContent = label; }, 1500);
        }
      }
    }
    overlay.querySelector('[data-copy]').onclick = function () {
      var e = errors[idx];
      if (!e) return;
      copyText(e.time + '\n' + e.text, this, '📋 复制');
    };
    overlay.querySelector('[data-copyall]').onclick = function () {
      var t = errors.map(function (e, i) {
        return '--- Error ' + (i + 1) + ' [' + e.time + '] ---\n' + e.text;
      }).join('\n\n');
      copyText(t, this, '📋 复制全部');
    };
    overlay.addEventListener('click', function (ev) {
      if (ev.target === overlay) overlay.style.display = 'none';
    });

    document.body.appendChild(overlay);
    return overlay;
  }

  function push(msg) {
    if (!msg) return;
    var now = new Date();
    var ts =
      String(now.getHours()).padStart(2, '0') + ':' +
      String(now.getMinutes()).padStart(2, '0') + ':' +
      String(now.getSeconds()).padStart(2, '0');
    errors.push({ time: ts, text: msg });
    idx = errors.length - 1;
    if (!document.body) {
      setTimeout(function () { push(''); }, 200);
      return;
    }
    var o = build();
    o.style.display = 'flex';
    render();
  }

  window.addEventListener('error', function (ev) {
    push(
      '[Error] ' + ev.message +
      '\n\nFile: ' + ev.filename +
      '\nLine: ' + ev.lineno + ', Col: ' + ev.colno +
      '\n\n' + (ev.error && ev.error.stack || '')
    );
  });

  window.addEventListener('unhandledrejection', function (ev) {
    var r = ev.reason;
    push(
      '[Unhandled Rejection]\n' +
      (r && r.message || r) +
      '\n\n' + (r && r.stack || '')
    );
  });

  var _origError = console.error;
  console.error = function () {
    _origError.apply(console, arguments);
    var parts = Array.prototype.map.call(arguments, function (x) {
      return typeof x === 'object' ? JSON.stringify(x, null, 2) : String(x);
    });
    var s = parts.join(' ');
    if (s.length < 5000) push('[console.error]\n' + s);
  };
})();
