// [plugin-admin 2026-08-23] Theme bridge for plugin panels (iframes).
// Why: an iframe is an independent document that cannot inherit host CSS
// variables, so panels previously had to copy theme values — and any host
// theme change or plugin styles override silently desynchronized them. How:
// auto-discover every custom property declared in :root/html rules across
// same-origin stylesheets, resolve each against the live cascade via
// getComputedStyle (so overrides from PluginStyles or future theme plugins
// are what gets injected), and set them onto the panel document's root
// element after it loads. Purpose: panel appearance follows the host theme
// at runtime, with zero values duplicated in plugin HTML.

export function collectHostCssVariables(): Record<string, string> {
  const vars: Record<string, string> = {};
  const rootStyle = getComputedStyle(document.documentElement);
  for (const sheet of Array.from(document.styleSheets)) {
    let rules: CSSRuleList;
    try {
      rules = sheet.cssRules;
    } catch {
      // cross-origin sheets (web fonts) are opaque; they never declare theme vars
      continue;
    }
    for (const rule of Array.from(rules)) {
      if (!(rule instanceof CSSStyleRule)) continue;
      const selector = rule.selectorText || '';
      // any rule that targets the document root, including selector lists
      if (!/(^|,)\s*(:root|html)\b/.test(selector)) continue;
      const declared = rule.style;
      for (let i = 0; i < declared.length; i++) {
        const prop = declared.item(i);
        if (!prop || !prop.startsWith('--')) continue;
        // computed value = final cascade, including plugin style overrides
        const value = rootStyle.getPropertyValue(prop).trim();
        if (value) vars[prop] = value;
      }
    }
  }
  return vars;
}

// [AutoC 2026-08-23] Rule-level sync. Why: injecting variable values alone
// left rule structure (scrollbar width, radius, border size) hardcoded in
// panel HTML — any host change to those values would silently desync again.
// How: serialize the host's own :root/html rules (variable declarations,
// including var()-to-var aliases) plus global chrome rules (`*` scrollbar
// styles and ::selection) verbatim as cssText. Purpose: panel chrome is the
// host's rule text, not a hand-copied approximation of it.
export function collectHostThemeCss(): string {
  const chunks: string[] = [];
  for (const sheet of Array.from(document.styleSheets)) {
    let rules: CSSRuleList;
    try {
      rules = sheet.cssRules;
    } catch {
      continue;
    }
    for (const rule of Array.from(rules)) {
      if (!(rule instanceof CSSStyleRule)) continue;
      const selector = rule.selectorText || '';
      const isRoot = /(^|,)\s*(:root|html)\b/.test(selector);
      const isGlobalChrome =
        selector === '*' ||
        selector.startsWith('*::-webkit-scrollbar') ||
        selector === '::selection';
      if (isRoot || isGlobalChrome) chunks.push(rule.cssText);
    }
  }
  return chunks.join('\n');
}

/** Copy the live host theme onto another same-origin document's root element. */
export function applyHostTheme(target: Document | null | undefined): void {
  if (!target) return;
  const vars = collectHostCssVariables();
  const rootStyle = target.documentElement.style;
  for (const [name, value] of Object.entries(vars)) {
    rootStyle.setProperty(name, value);
  }
  // host rule text rides in one owned <style> element; appended last so host
  // rules win over any pre-injection fallback styles in the panel page
  let styleEl = target.querySelector('style[data-clonoth-host-theme]');
  if (!styleEl) {
    styleEl = target.createElement('style');
    styleEl.setAttribute('data-clonoth-host-theme', '');
    target.head.appendChild(styleEl);
  }
  styleEl.textContent = collectHostThemeCss();
}
