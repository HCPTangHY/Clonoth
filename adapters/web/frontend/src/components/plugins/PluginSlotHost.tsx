// [AutoC 2026-08-22] Plugin slot host.
// Why: slots are plugin contributions rendered inside existing UI at host-reserved
// positions; the plugin never touches host DOM beyond the element it is granted.
// How: for each contribution, import the declared ES module source from a Blob
// URL, call its default export (function = mount shorthand; object =
// mount/update/destroy), and keep React away from the children — React renders
// only the container div and never reconciles plugin-written nodes. Errors in
// one contribution are isolated and never break the surrounding page.
// Purpose: small widgets (badges, counters) extend the chat UI without iframes.
import { useEffect, useRef } from 'react';

import { usePluginsStore, type SlotContribution } from '../../store/pluginsStore';

interface SlotModule {
  mount?: (ctx: { el: HTMLElement; data?: Record<string, unknown> }) => void;
  update?: (ctx: { el: HTMLElement; data?: Record<string, unknown> }) => void;
  destroy?: () => void;
}

interface SlotInstance {
  contribution: SlotContribution;
  mod: SlotModule | null;
  blobUrl: string;
  failed: boolean;
}

interface PluginSlotHostProps {
  slot: string;
  data?: Record<string, unknown>;
  className?: string;
}

export const PluginSlotHost = ({ slot, data, className }: PluginSlotHostProps) => {
  const contributions = usePluginsStore((s) => s.slotsBySlot[slot]);
  const clientScriptsEnabled = usePluginsStore((s) => s.clientScriptsEnabled);
  const containersRef = useRef<Map<string, HTMLDivElement>>(new Map());
  const instancesRef = useRef<Map<string, SlotInstance>>(new Map());

  const list: SlotContribution[] = clientScriptsEnabled ? contributions || [] : [];

  useEffect(() => {
    const instances = instancesRef.current;
    const containers = containersRef.current;
    const alive = new Set(list.map((c) => c.slotId));

    // destroy contributions that disappeared (plugin unloaded or scripts disabled)
    for (const [slotId, inst] of instances) {
      if (alive.has(slotId)) continue;
      try {
        inst.mod?.destroy?.();
      } catch {
        /* plugin errors stay isolated */
      }
      URL.revokeObjectURL(inst.blobUrl);
      instances.delete(slotId);
      containers.delete(slotId);
    }

    for (const contribution of list) {
      const el = containers.get(contribution.slotId);
      if (!el) continue; // container not rendered yet; the next effect run picks it up
      let inst = instances.get(contribution.slotId);
      if (!inst) {
        const blobUrl = URL.createObjectURL(
          new Blob([contribution.script], { type: 'text/javascript' }),
        );
        const fresh: SlotInstance = { contribution, mod: null, blobUrl, failed: false };
        inst = fresh;
        instances.set(contribution.slotId, fresh);
        import(/* @vite-ignore */ blobUrl)
          .then((mod) => {
            if (instances.get(contribution.slotId) !== fresh) return; // replaced meanwhile
            const def: unknown = (mod as { default?: unknown }).default;
            fresh.mod = typeof def === 'function'
              ? { mount: def as SlotModule['mount'] }
              : def && typeof def === 'object'
                ? (def as SlotModule)
                : null;
            if (!fresh.mod) {
              fresh.failed = true;
              return;
            }
            try {
              fresh.mod.mount?.({ el, data });
            } catch (err) {
              console.error(`[plugin-slot:${contribution.slotId}] mount failed`, err);
            }
          })
          .catch((err) => {
            fresh.failed = true;
            console.error(`[plugin-slot:${contribution.slotId}] import failed`, err);
          });
        continue;
      }
      if (inst.mod && !inst.failed) {
        try {
          inst.mod.update?.({ el, data });
        } catch {
          /* plugin errors stay isolated */
        }
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list, data]);

  // final teardown when the host unmounts
  useEffect(
    () => () => {
      for (const inst of instancesRef.current.values()) {
        try {
          inst.mod?.destroy?.();
        } catch {
          /* plugin errors stay isolated */
        }
        URL.revokeObjectURL(inst.blobUrl);
      }
      instancesRef.current.clear();
    },
    [],
  );

  if (list.length === 0) return null;

  return (
    <div className={className}>
      {list.map((contribution) => (
        <div
          key={contribution.slotId}
          data-plugin-slot={contribution.slotId}
          ref={(el) => {
            if (el) containersRef.current.set(contribution.slotId, el);
            else containersRef.current.delete(contribution.slotId);
          }}
        />
      ))}
    </div>
  );
};
