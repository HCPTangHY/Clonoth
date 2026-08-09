// [2026-06-01] Modal wrapper for session configuration.
// [2026-06-04] Refactored to use the shared Modal shell.
import { SessionConfigPanel } from './SessionConfigPanel';
import { Modal } from '../common';

interface SessionConfigModalProps {
  sessionId: string;
  focus: 'node' | 'model';
  onClose: () => void;
}

export const SessionConfigModal = ({ sessionId, focus, onClose }: SessionConfigModalProps) => {
  return (
    <Modal
      onClose={onClose}
      open={true}
      subtitle="会话编辑"
      title={focus === 'node' ? '节点配置' : '模型配置'}
    >
      <SessionConfigPanel focus={focus} sessionId={sessionId} />
    </Modal>
  );
};
