// [AutoC 2026-08-22] Plugin styles injector.
// Why: style-tier contributions are pure CSS strings declared in PLUGIN_META —
// no script execution, no iframe. How: sync <style data-plugin="owner"> tags in
// document.head with the store's stylesByOwner map; tags whose owner vanished
// are removed. Purpose: theming and micro-adjustments with the lightest possible
// footprint, and unload removes the effect entirely.
import { useEffect } from 'react';

import { usePluginsStore } from '../../store/pluginsStore';

const styleTagId = (owner: string) => `plugin-style-${owner}`;

export const PluginStyles = () => {
  const stylesByOwner = usePluginsStore((s) => s.stylesByOwner);

  useEffect(() => {
    const wanted = new Set(Object.keys(stylesByOwner));
    // remove tags whose owner disappeared (plugin unloaded)
    document.querySelectorAll<HTMLStyleElement>('style[data-plugin]').forEach((tag) => {
      const owner = tag.dataset.plugin || '';
      if (!wanted.has(owner)) tag.remove();
    });
    for (const [owner, css] of Object.entries(stylesByOwner)) {
      let tag = document.getElementById(styleTagId(owner)) as HTMLStyleElement | null;
      if (!tag) {
        tag = document.createElement('style');
        tag.id = styleTagId(owner);
        tag.dataset.plugin = owner;
        document.head.appendChild(tag);
      }
      if (tag.textContent !== css) tag.textContent = css;
    }
  }, [stylesByOwner]);

  return null;
};
