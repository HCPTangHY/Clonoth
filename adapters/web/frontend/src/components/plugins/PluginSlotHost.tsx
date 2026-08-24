// [AutoC 2026-08-22] Plugin slot host.
// Why: slots are plugin contributions rendered inside existing UI at host-reserved
// positions; the plugin never touches host DOM beyond the element it is granted.
// How: for each contribution, import the declared ES module source from a Blob
// URL, call its default export (function = mount shorthand; object =
// mount/update/destroy), and keep React away from the children — React renders
// only the container div and never reconciles plugin-written nodes. Errors in
// one contribution are isolated and never break the surrounding page.
// [AutoC 2026-08-22] Context v2: mount/update receive { el, data, api, state,
// events }. `state` is plugin-private and survives remounts (wiped only when the
// contribution disappears); `events` is a scoped subscriber whose subscriptions
// are auto-revoked on destroy. A contribution may declare mode 'replace' to take
// over the whole slot region — the highest-priority replace wins and all other
// contributions in that slot are not mounted.
// Purpose: small widgets (badges, counters) extend the chat UI without iframes.
import { useEffect, useMemo, useRef } from 'react';

import { usePluginsStore, type SlotContribution } from '../../store/pluginsStore';
import { getPluginSlotApi, type PluginSlotApi } from '../../store/slotApi';
import {
  getPluginState,
  subscribePluginEvent,
  wipePluginState,
  type PluginEventEmitter,
} from '../../store/pluginRuntime';

interface SlotContextShape {
  el: HTMLElement;
  data?: Record<string, unknown>;
  api?: PluginSlotApi;
  /** Plugin-private state; survives host remounts, wiped on contribution removal. */
  state: Record<string, unknown>;
  /** Scoped event subscriber; subscriptions auto-revoked when this instance dies. */
  events: PluginEventEmitter;
}

interface SlotModule {
  mount?: (ctx: SlotContextShape) => void;
  update?: (ctx: SlotContextShape) => void;
  destroy?: () => void;
}

interface SlotInstance {
  contribution: SlotContribution;
  mod: SlotModule | null;
  blobUrl: string;
  failed: boolean;
  eventDisposers: Set<() => void>;
}

interface PluginSlotHostProps {
  slot: string;
  data?: Record<string, unknown>;
  className?: string;
}

// [AutoC 2026-08-22] Why: `contributions || []` mints a new array identity every
// render when the slot is empty, firing the effect below needlessly. How: one
// module-level empty constant for both empty cases.
const EMPTY: SlotContribution[] = [];

export const PluginSlotHost = ({ slot, data, className }: PluginSlotHostProps) => {
  const contributions = usePluginsStore((s) => s.slotsBySlot[slot]);
  const clientScriptsEnabled = usePluginsStore((s) => s.clientScriptsEnabled);
  const containersRef = useRef<Map<string, HTMLDivElement>>(new Map());
  const instancesRef = useRef<Map<string, SlotInstance>>(new Map());

  const all: SlotContribution[] = clientScriptsEnabled ? contributions ?? EMPTY : EMPTY;

  // replace 模式：声明 replace 的贡献接管整个槽位区域，取优先级最高者。
  const list = useMemo(() => {
    const idx = all.findIndex((c) => c.mode === 'replace');
    return idx >= 0 ? [all[idx]] : all;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [all]);

  const scopedEvents = (inst: SlotInstance): PluginEventEmitter => ({
    on: (type, handler) => {
      const dispose = subscribePluginEvent(type, handler);
      inst.eventDisposers.add(dispose);
      return dispose;
    },
  });

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
      for (const dispose of inst.eventDisposers) dispose();
      instances.delete(slotId);
      containers.delete(slotId);
      // contribution gone for good — its private state dies with it
      wipePluginState(slotId);
    }

    for (const contribution of list) {
      const el = containers.get(contribution.slotId);
      if (!el) continue; // container not rendered yet; the next effect run picks it up
      let inst = instances.get(contribution.slotId);
      if (!inst) {
        const blobUrl = URL.createObjectURL(
          new Blob([contribution.script], { type: 'text/javascript' }),
        );
        const fresh: SlotInstance = {
          contribution,
          mod: null,
          blobUrl,
          failed: false,
          eventDisposers: new Set(),
        };
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
              fresh.mod.mount?.({
                el,
                data,
                api: getPluginSlotApi(),
                state: getPluginState(contribution.slotId),
                events: scopedEvents(fresh),
              });
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
          inst.mod.update?.({
            el,
            data,
            api: getPluginSlotApi(),
            state: getPluginState(contribution.slotId),
            events: scopedEvents(inst),
          });
        } catch {
          /* plugin errors stay isolated */
        }
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list, data]);

  // final teardown when the host unmounts (state and events revocation only —
  // private state intentionally survives remounts)
  useEffect(
    () => () => {
      for (const inst of instancesRef.current.values()) {
        try {
          inst.mod?.destroy?.();
        } catch {
          /* plugin errors stay isolated */
        }
        URL.revokeObjectURL(inst.blobUrl);
        for (const dispose of inst.eventDisposers) dispose();
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
          // [AutoC 2026-08-24] display:contents 让该容器不生成盒子，插件内容
          // 直接参与父级 flex 布局，与同级宿主元素（复制/信息按钮）基线对齐。
          style={{ display: 'contents' }}
          ref={(el) => {
            if (el) containersRef.current.set(contribution.slotId, el);
            else containersRef.current.delete(contribution.slotId);
          }}
        />
      ))}
    </div>
  );
};
