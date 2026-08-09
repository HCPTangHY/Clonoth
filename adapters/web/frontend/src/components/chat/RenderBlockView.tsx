// [2026-05-31] RenderBlockView dispatches normalized render blocks to v2 views.
// Why: MessageCard should not know every block's internal rendering details. How:
// switch on RenderBlock.kind and pass tool IDs through the toolsById prop supplied by
// the caller. Purpose: keep block-specific rendering isolated while preserving one
// ordered message body.
import type { RenderBlock, ToolExecution } from '../../types/message';
import { ApprovalBlockView } from './ApprovalBlockView';
import { NoticeBlockView } from './NoticeBlockView';
import { TextBlockView } from './TextBlockView';
import { ThinkingBlock } from './ThinkingBlock';
import { ToolCallCard } from './ToolCallCard';
import type { MessageRenderContext } from './renderingConstants';

interface RenderBlockViewProps {
  block: RenderBlock;
  toolsById: Record<string, ToolExecution>;
  messageContext?: MessageRenderContext;
}

export const RenderBlockView = ({ block, toolsById, messageContext }: RenderBlockViewProps) => {
  if (block.kind === 'text') {
    // Why: text block styling depends on the surrounding message role, status, and
    // completion type together. How: pass one context object instead of three separate
    // props. Purpose: future text markers cannot drift between MessageCard and this
    // dispatcher.
    return <TextBlockView block={block} messageContext={messageContext} />;
  }

  if (block.kind === 'thinking') {
    return <ThinkingBlock block={block} />;
  }

  if (block.kind === 'tool') {
    const tools = block.toolIds.map((toolId) => toolsById[toolId]).filter((tool): tool is ToolExecution => Boolean(tool) && !tool.hidden);
    if (tools.length === 0) return null;

    return (
      <div className="space-y-1.5">
        {tools.map((tool) => (
          <ToolCallCard key={tool.stableId} tool={tool} />
        ))}
      </div>
    );
  }

  if (block.kind === 'approval') {
    return <ApprovalBlockView block={block} />;
  }

  return <NoticeBlockView block={block} />;
};

export type { RenderBlockViewProps };
