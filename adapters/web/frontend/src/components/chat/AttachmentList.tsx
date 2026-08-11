// [AutoC 2026-06-17] Shared attachment renderer for chat messages and composer previews.
// [AutoC 2026-08-11] Rewritten to fetch attachments via authenticated JS requests
// instead of bare <img src> / <a href>.  The backend /v1/attachments/file now
// requires admin token, so we fetch → blob URL for rendering and downloads.
import { useEffect, useState } from 'react';
import { Icon } from '../common';
import { useSettingsStore } from '../../store/settingsStore';

export type RenderableAttachment = {
  name?: string;
  size?: number;
  url?: string;
  type?: string;
  path?: string;
  mime_type?: string;
};

interface AttachmentListProps {
  attachments: readonly RenderableAttachment[];
  variant?: 'message' | 'composer';
  sessionId?: string;
  onRemove?: (index: number) => void;
}

const IMAGE_EXTENSIONS = new Set(['.apng', '.avif', '.bmp', '.gif', '.jpeg', '.jpg', '.png', '.svg', '.webp']);
const ARCHIVE_EXTENSIONS = new Set(['.7z', '.gz', '.rar', '.tar', '.tgz', '.zip']);
const TEXT_EXTENSIONS = new Set(['.csv', '.json', '.log', '.md', '.py', '.ts', '.tsx', '.txt', '.yaml', '.yml']);

/** Clean up a path value — strip file:// prefix, normalise slashes.
 *  No whitelist filtering: access control is enforced server-side
 *  by /v1/sessions/{id}/file (workspace containment + data/ check). */
function cleanPath(value: string | undefined): string {
  return (value || '').replace(/\\/g, '/').replace(/^file:\/\//, '').trim();
}

function getExtension(value: string): string {
  const clean = value.split('?')[0].split('#')[0];
  const dot = clean.lastIndexOf('.');
  return dot >= 0 ? clean.slice(dot).toLowerCase() : '';
}

function safeDecodeURIComponent(value: string): string {
  try { return decodeURIComponent(value); } catch { return value; }
}

export function getAttachmentName(attachment: RenderableAttachment): string {
  const explicit = (attachment.name || '').trim();
  if (explicit) return explicit;
  const path = cleanPath(attachment.path) || cleanPath(attachment.url);
  if (path) return safeDecodeURIComponent(path.split('/').pop() || '附件');
  return '附件';
}

/** Build the API path for fetching a file through the session endpoint. */
function getAttachmentApiPath(attachment: RenderableAttachment, sessionId?: string): string | undefined {
  const url = (attachment.url || '').trim();
  if (url && /^(blob:|data:)/i.test(url)) return undefined;
  if (url && /^https?:\/\//i.test(url)) return undefined;
  const servePath = cleanPath(attachment.path) || cleanPath(url);
  if (!servePath) return undefined;
  const sid = sessionId || '_default';
  return `/v1/sessions/${encodeURIComponent(sid)}/file?path=${encodeURIComponent(servePath)}`;
}

/** Return a direct URL that needs no fetch (blob, data, external). */
function getDirectUrl(attachment: RenderableAttachment): string | undefined {
  const url = (attachment.url || '').trim();
  if (url && /^(blob:|data:|https?:\/\/)/i.test(url)) return url;
  return undefined;
}

export function isImageAttachment(attachment: RenderableAttachment): boolean {
  if (attachment.type === 'image') return true;
  if ((attachment.mime_type || '').toLowerCase().startsWith('image/')) return true;
  const name = getAttachmentName(attachment);
  const path = cleanPath(attachment.path) || cleanPath(attachment.url);
  return IMAGE_EXTENSIONS.has(getExtension(name)) || IMAGE_EXTENSIONS.has(getExtension(path));
}

// Re-export for external consumers that only need the href (e.g. history hydration)
export function getAttachmentHref(attachment: RenderableAttachment): string | undefined {
  return getDirectUrl(attachment) || getAttachmentApiPath(attachment);
}

function getFileIcon(attachment: RenderableAttachment): string {
  const mime = (attachment.mime_type || '').toLowerCase();
  const name = getAttachmentName(attachment);
  const ext = getExtension(name);
  if (isImageAttachment(attachment)) return 'image';
  if (mime.startsWith('audio/')) return 'audio_file';
  if (mime.startsWith('video/')) return 'movie';
  if (mime === 'application/pdf' || ext === '.pdf') return 'picture_as_pdf';
  if (ARCHIVE_EXTENSIONS.has(ext)) return 'folder_zip';
  if (TEXT_EXTENSIONS.has(ext) || mime.startsWith('text/')) return 'description';
  return 'attach_file';
}

function formatAttachmentSize(size: number | undefined): string {
  if (!size || size <= 0) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.max(1, Math.round(size / 1024))} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function getAttachmentMeta(attachment: RenderableAttachment): string {
  const size = formatAttachmentSize(attachment.size);
  const mime = (attachment.mime_type || '').trim();
  if (size && mime) return `${size} · ${mime}`;
  return size || mime;
}

// ── Blob cache (module-level, survives re-renders) ──────────────────────
const blobCache = new Map<string, string>();

async function fetchBlobUrl(apiPath: string, token: string): Promise<string> {
  const cached = blobCache.get(apiPath);
  if (cached) return cached;
  const resp = await fetch(apiPath, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok) throw new Error(`${resp.status}`);
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  blobCache.set(apiPath, url);
  return url;
}

// ── Per-attachment hook ─────────────────────────────────────────────────
function useAuthenticatedUrl(attachment: RenderableAttachment, sessionId?: string): { url: string | undefined; loading: boolean } {
  const adminToken = useSettingsStore(s => s.adminToken);
  const directUrl = getDirectUrl(attachment);
  const apiPath = getAttachmentApiPath(attachment, sessionId);
  const [blobUrl, setBlobUrl] = useState<string | undefined>(() => {
    // Synchronous cache hit avoids flicker on re-render
    if (apiPath && blobCache.has(apiPath)) return blobCache.get(apiPath);
    return undefined;
  });
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (directUrl || !apiPath || !adminToken) return;
    if (blobCache.has(apiPath)) {
      setBlobUrl(blobCache.get(apiPath));
      return;
    }
    let cancelled = false;
    setLoading(true);
    fetchBlobUrl(apiPath, adminToken).then(
      url => { if (!cancelled) { setBlobUrl(url); setLoading(false); } },
      () => { if (!cancelled) setLoading(false); },
    );
    return () => { cancelled = true; };
  }, [directUrl, apiPath, adminToken]);

  if (directUrl) return { url: directUrl, loading: false };
  return { url: blobUrl, loading };
}

// ── Single attachment item ──────────────────────────────────────────────
function AttachmentItem({
  attachment,
  index,
  isComposer,
  sessionId,
  onRemove,
}: {
  attachment: RenderableAttachment;
  index: number;
  isComposer: boolean;
  sessionId?: string;
  onRemove?: (index: number) => void;
}) {
  const name = getAttachmentName(attachment);
  const meta = getAttachmentMeta(attachment);
  const { url, loading } = useAuthenticatedUrl(attachment, isComposer ? undefined : sessionId);
  const isImage = isImageAttachment(attachment);

  const removeButton = onRemove ? (
    <button
      aria-label={`移除附件 ${name}`}
      className="absolute right-1 top-1 inline-flex h-6 w-6 items-center justify-center rounded-sm bg-black/60 text-white opacity-90 transition-opacity hover:opacity-100"
      onClick={() => onRemove(index)}
      type="button"
    >
      <Icon name="close" size={14} />
    </button>
  ) : null;

  const imageClass = isComposer
    ? 'h-20 w-20 object-cover'
    : 'max-h-72 max-w-full rounded-sm object-contain';
  const imageShellClass = isComposer
    ? 'group relative overflow-hidden border border-[var(--duties-border)] bg-[var(--duties-panel)]'
    : 'group relative inline-block overflow-hidden border border-[var(--duties-border)] bg-[var(--duties-panel)]';

  if (isImage) {
    if (loading) {
      return (
        <figure className={imageShellClass} title={name}>
          <div className={`${isComposer ? 'h-20 w-20' : 'h-32 w-32'} flex items-center justify-center text-[var(--duties-tertiary)]`}>
            <Icon name="hourglass_empty" size={20} />
          </div>
          <figcaption className="max-w-56 truncate px-2 py-1 text-[0.68rem] text-[var(--duties-secondary)]">{name}</figcaption>
        </figure>
      );
    }
    if (url) {
      const image = <img alt={name} className={imageClass} loading="lazy" src={url} />;
      return (
        <figure className={imageShellClass} title={meta ? `${name} · ${meta}` : name}>
          {isComposer ? image : <a href={url} rel="noopener noreferrer" target="_blank">{image}</a>}
          {removeButton}
          <figcaption className="max-w-56 truncate px-2 py-1 text-[0.68rem] text-[var(--duties-secondary)]">
            <span>{name}</span>
            {meta && <span className="text-[var(--duties-tertiary)]"> · {meta}</span>}
          </figcaption>
        </figure>
      );
    }
  }

  // File attachment (non-image, or image without URL)
  const fileInner = (
    <>
      <Icon name={loading ? 'hourglass_empty' : getFileIcon(attachment)} size={18} />
      <span className="min-w-0 flex-1 truncate">{name}</span>
      {meta && <span className="shrink-0 text-[var(--duties-tertiary)]">{meta}</span>}
    </>
  );
  const fileClass = `relative inline-flex ${isComposer ? 'max-w-72' : 'max-w-full'} items-center gap-2 border border-[var(--duties-border)] bg-[var(--duties-panel)] px-2 py-1.5 text-xs text-[var(--duties-secondary)] transition-colors hover:border-[var(--duties-text)] hover:text-[var(--duties-text)] ${onRemove ? 'pr-8' : ''}`;

  if (url && !onRemove) {
    return (
      <a className={fileClass} download={name} href={url} rel="noopener noreferrer" target="_blank" title={meta ? `${name} · ${meta}` : name}>
        {fileInner}
      </a>
    );
  }
  return (
    <div className={`${fileClass} ${url ? '' : loading ? '' : 'opacity-75'}`} title={meta ? `${name} · ${meta}` : name}>
      {fileInner}
      {removeButton}
    </div>
  );
}

// ── List component ──────────────────────────────────────────────────────
export function AttachmentList({ attachments, variant = 'message', sessionId, onRemove }: AttachmentListProps) {
  if (attachments.length === 0) return null;

  const isComposer = variant === 'composer';
  const listClass = isComposer ? 'mb-2 flex flex-wrap gap-2' : 'mt-2 flex flex-wrap gap-2';

  return (
    <div className={listClass}>
      {attachments.map((attachment, index) => (
        <AttachmentItem
          key={`${getAttachmentName(attachment)}-${attachment.path || attachment.url || index}`}
          attachment={attachment}
          index={index}
          isComposer={isComposer}
          sessionId={sessionId}
          onRemove={onRemove}
        />
      ))}
    </div>
  );
}
