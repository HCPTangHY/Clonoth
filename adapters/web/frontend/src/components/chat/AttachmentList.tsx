// [AutoC 2026-06-17] Shared attachment renderer for chat messages and composer previews.
// Why: inbound user files, outbound tool images, and pasted local files all share the
// same metadata shape but used to render through separate ad-hoc snippets. How: keep
// URL resolution, image detection, file labels, and remove buttons in one component.
// Purpose: live messages, refreshed history, and local drafts show attachments the
// same way.
import { Icon } from '../common';

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
  onRemove?: (index: number) => void;
}

const IMAGE_EXTENSIONS = new Set(['.apng', '.avif', '.bmp', '.gif', '.jpeg', '.jpg', '.png', '.svg', '.webp']);
const ARCHIVE_EXTENSIONS = new Set(['.7z', '.gz', '.rar', '.tar', '.tgz', '.zip']);
const TEXT_EXTENSIONS = new Set(['.csv', '.json', '.log', '.md', '.py', '.ts', '.tsx', '.txt', '.yaml', '.yml']);

function normalizeAttachmentPath(value: string | undefined): string {
  const raw = (value || '').replace(/\\/g, '/').replace(/^file:\/\//, '').replace(/^\/+/, '').trim();
  if (raw.startsWith('data/attachments/')) return raw;
  // [AutoC 2026-06-17] Legacy generated-image rows stored paths under data/temp
  // before media tools were normalized into data/attachments. Keep serving only
  // image-like temp files so refreshed old history can show those pictures without
  // making arbitrary data/temp documents downloadable from the attachment UI.
  if (raw.startsWith('data/temp/') && /\.(apng|avif|bmp|gif|jpe?g|png|svg|webp)$/i.test(raw.split('?')[0].split('#')[0])) return raw;
  return '';
}

function getPathFromUrl(url: string | undefined): string {
  if (!url) return '';
  return normalizeAttachmentPath(url);
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
  const path = normalizeAttachmentPath(attachment.path) || getPathFromUrl(attachment.url);
  if (path) return safeDecodeURIComponent(path.split('/').pop() || '附件');
  return '附件';
}

export function getAttachmentHref(attachment: RenderableAttachment): string | undefined {
  const url = (attachment.url || '').trim();
  if (url && (/^(blob:|data:|https?:\/\/)/i.test(url) || url.startsWith('/v1/attachments/file'))) {
    return url;
  }
  const path = normalizeAttachmentPath(attachment.path) || getPathFromUrl(url);
  if (path) return `/v1/attachments/file?path=${encodeURIComponent(path)}`;
  return url || undefined;
}

export function isImageAttachment(attachment: RenderableAttachment): boolean {
  if (attachment.type === 'image') return true;
  if ((attachment.mime_type || '').toLowerCase().startsWith('image/')) return true;
  const name = getAttachmentName(attachment);
  const path = normalizeAttachmentPath(attachment.path) || getPathFromUrl(attachment.url);
  return IMAGE_EXTENSIONS.has(getExtension(name)) || IMAGE_EXTENSIONS.has(getExtension(path));
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

export function AttachmentList({ attachments, variant = 'message', onRemove }: AttachmentListProps) {
  if (attachments.length === 0) return null;

  const isComposer = variant === 'composer';
  const imageClass = isComposer
    ? 'h-20 w-20 object-cover'
    : 'max-h-72 max-w-full rounded-sm object-contain';
  const imageShellClass = isComposer
    ? 'group relative overflow-hidden border border-[var(--duties-border)] bg-[var(--duties-panel)]'
    : 'group relative inline-block overflow-hidden border border-[var(--duties-border)] bg-[var(--duties-panel)]';
  const listClass = isComposer ? 'mb-2 flex flex-wrap gap-2' : 'mt-2 flex flex-wrap gap-2';

  return (
    <div className={listClass}>
      {attachments.map((attachment, index) => {
        const name = getAttachmentName(attachment);
        const meta = getAttachmentMeta(attachment);
        const href = getAttachmentHref(attachment);
        const key = `${name}-${attachment.path || attachment.url || index}`;
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

        if (isImageAttachment(attachment) && href) {
          const image = <img alt={name} className={imageClass} loading="lazy" src={href} />;
          return (
            <figure key={key} className={imageShellClass} title={meta ? `${name} · ${meta}` : name}>
              {isComposer ? image : <a href={href} rel="noopener noreferrer" target="_blank">{image}</a>}
              {removeButton}
              <figcaption className="max-w-56 truncate px-2 py-1 text-[0.68rem] text-[var(--duties-secondary)]">
                <span>{name}</span>
                {meta && <span className="text-[var(--duties-tertiary)]"> · {meta}</span>}
              </figcaption>
            </figure>
          );
        }

        const fileInner = (
          <>
            <Icon name={getFileIcon(attachment)} size={18} />
            <span className="min-w-0 flex-1 truncate">{name}</span>
            {meta && <span className="shrink-0 text-[var(--duties-tertiary)]">{meta}</span>}
          </>
        );
        const fileClass = `relative inline-flex ${isComposer ? 'max-w-72' : 'max-w-full'} items-center gap-2 border border-[var(--duties-border)] bg-[var(--duties-panel)] px-2 py-1.5 text-xs text-[var(--duties-secondary)] transition-colors hover:border-[var(--duties-text)] hover:text-[var(--duties-text)] ${onRemove ? 'pr-8' : ''}`;

        return href && !onRemove ? (
          <a key={key} className={fileClass} download={name} href={href} rel="noopener noreferrer" target="_blank" title={meta ? `${name} · ${meta}` : name}>
            {fileInner}
          </a>
        ) : (
          <div key={key} className={`${fileClass} ${href ? '' : 'opacity-75'}`} title={meta ? `${name} · ${meta}` : name}>
            {fileInner}
            {removeButton}
          </div>
        );
      })}
    </div>
  );
}
