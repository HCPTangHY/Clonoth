// [2026-05-31] NoticeBlockView renders reducer notices inside MessageCard v2.
// Why: reducer events such as retries, warnings, and errors need a compact place in
// the same message stream as text and tools. How: map NoticeBlock levels to left-border
// colors and simple icons while preserving optional titles. Purpose: avoid reintroducing
// a separate live-only preview channel for operational messages.
import type { NoticeBlock } from '../../types/message';
import { Icon } from '../common';
import { INLINE_BLOCK_BODY_TEXT_CLASS, INLINE_BLOCK_INDENT_CLASS } from './renderingConstants';

interface NoticeBlockViewProps {
  block: NoticeBlock;
}

const LEVEL_STYLES: Record<string, { icon: string; className: string; titleClassName: string }> = {
  info: {
    icon: 'info',
    className: 'border-blue-400 bg-blue-50 text-blue-800',
    titleClassName: 'text-blue-900',
  },
  warning: {
    icon: 'warning',
    className: 'border-orange-400 bg-orange-50 text-orange-800',
    titleClassName: 'text-orange-900',
  },
  error: {
    icon: 'error',
    className: 'border-red-400 bg-red-50 text-red-800',
    titleClassName: 'text-red-900',
  },
  preempt: {
    icon: 'quick_phrases',
    className: 'border-violet-400 bg-transparent text-[var(--duties-text)]',
    titleClassName: 'text-violet-600',
  },
};

export const NoticeBlockView = ({ block }: NoticeBlockViewProps) => {
  const style = LEVEL_STYLES[block.level] || LEVEL_STYLES.info;
  const isPreempt = block.level === 'preempt';

  if (isPreempt) {
    return (
      <div className={`${INLINE_BLOCK_INDENT_CLASS} border-l-2 border-violet-400 pl-3 py-1.5 font-mono ${INLINE_BLOCK_BODY_TEXT_CLASS}`}>
        <div className="flex items-center gap-1.5 text-violet-500">
          <Icon name="quick_phrases" size={14} />
          <span className="font-semibold">追加指令</span>
        </div>
        <div className="mt-1 whitespace-pre-wrap break-words text-[var(--duties-text)]">{block.text}</div>
      </div>
    );
  }

  return (
    <div className={`border-l-2 px-3 py-2 text-xs ${style.className}`}>
      <div className="flex items-start gap-2">
        <span className="mt-0.5 font-mono">
          <Icon name={style.icon} size={14} />
        </span>
        <div className="min-w-0 flex-1">
          {block.title && <div className={`mb-0.5 font-mono font-semibold ${style.titleClassName}`}>{block.title}</div>}
          <div className="whitespace-pre-wrap break-words">{block.text}</div>
        </div>
      </div>
    </div>
  );
};
