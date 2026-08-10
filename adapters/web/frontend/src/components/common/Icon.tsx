import { type ComponentType, type SVGProps } from 'react';

// [2026-06-02] Migrate the shared icon primitive from the Material Symbols font to SVG components.
// Why: the font-based ligature path required a custom subset script and a generated woff2 asset.
// How: each icon currently used by the frontend is imported through a single-icon deep path and stored in ICON_MAP.
// Purpose: the public Icon API remains stable while builds no longer depend on a font file or prebuild step.
import { ApprovalW400 } from '@material-symbols-svg/react/icons/approval';
import { ArrowBackW400 } from '@material-symbols-svg/react/icons/arrow-back';
import { AttachFileW400 } from '@material-symbols-svg/react/icons/attach-file';
import { BuildW400 } from '@material-symbols-svg/react/icons/build';
import { CableW400 } from '@material-symbols-svg/react/icons/cable';
import { CancelW400 } from '@material-symbols-svg/react/icons/cancel';
import { CheckCircleW400 } from '@material-symbols-svg/react/icons/check-circle';
import { ChevronLeftW400 } from '@material-symbols-svg/react/icons/chevron-left';
import { ChevronRightW400 } from '@material-symbols-svg/react/icons/chevron-right';
import { CloseW400 } from '@material-symbols-svg/react/icons/close';
import { CodeW400 } from '@material-symbols-svg/react/icons/code';
import { DeleteW400 } from '@material-symbols-svg/react/icons/delete';
import { EditW400 } from '@material-symbols-svg/react/icons/edit';
import { DisplaySettingsW400 } from '@material-symbols-svg/react/icons/display-settings';
import { DraftW400 } from '@material-symbols-svg/react/icons/draft';
import { ErrorW400 } from '@material-symbols-svg/react/icons/error';
import { FolderW400 } from '@material-symbols-svg/react/icons/folder';
import { HubW400 } from '@material-symbols-svg/react/icons/hub';
import { InboxW400 } from '@material-symbols-svg/react/icons/inbox';
import { InfoW400 } from '@material-symbols-svg/react/icons/info';
import { KeyboardReturnW400 } from '@material-symbols-svg/react/icons/keyboard-return';
import { MenuW400 } from '@material-symbols-svg/react/icons/menu';
import { MenuBookW400 } from '@material-symbols-svg/react/icons/menu-book';
import { ModelTrainingW400 } from '@material-symbols-svg/react/icons/model-training';
import { OpenInNewW400 } from '@material-symbols-svg/react/icons/open-in-new';
import { PendingW400 } from '@material-symbols-svg/react/icons/pending';
import { ProgressActivityW400 } from '@material-symbols-svg/react/icons/progress-activity';
import { RefreshW400 } from '@material-symbols-svg/react/icons/refresh';
import { ScheduleW400 } from '@material-symbols-svg/react/icons/schedule';
import { SettingsW400 } from '@material-symbols-svg/react/icons/settings';
import { SettingsPowerW400 } from '@material-symbols-svg/react/icons/settings-power';
import { SmartToyW400 } from '@material-symbols-svg/react/icons/smart-toy';
import { TimerW400 } from '@material-symbols-svg/react/icons/timer';
import { TuneW400 } from '@material-symbols-svg/react/icons/tune';
import { VerifiedUserW400 } from '@material-symbols-svg/react/icons/verified-user';
import { WarningW400 } from '@material-symbols-svg/react/icons/warning';

// [2026-06-05] Tool-specific icons for the compact ToolCallCard layout.
import { BookmarkW400 } from '@material-symbols-svg/react/icons/bookmark';
import { BookmarksW400 } from '@material-symbols-svg/react/icons/bookmarks';
import { BookmarkRemoveW400 } from '@material-symbols-svg/react/icons/bookmark-remove';
import { BugReportW400 } from '@material-symbols-svg/react/icons/bug-report';
import { CalculateW400 } from '@material-symbols-svg/react/icons/calculate';
import { CalendarMonthW400 } from '@material-symbols-svg/react/icons/calendar-month';
import { ChatBubbleW400 } from '@material-symbols-svg/react/icons/chat-bubble';
import { CompressW400 } from '@material-symbols-svg/react/icons/compress';
import { DataUsageW400 } from '@material-symbols-svg/react/icons/data-usage';
import { DeleteSweepW400 } from '@material-symbols-svg/react/icons/delete-sweep';
import { DescriptionW400 } from '@material-symbols-svg/react/icons/description';
import { DifferenceW400 } from '@material-symbols-svg/react/icons/difference';
import { DnsW400 } from '@material-symbols-svg/react/icons/dns';
import { EditDocumentW400 } from '@material-symbols-svg/react/icons/edit-document';
import { EngineeringW400 } from '@material-symbols-svg/react/icons/engineering';
import { EventBusyW400 } from '@material-symbols-svg/react/icons/event-busy';
import { ExploreW400 } from '@material-symbols-svg/react/icons/explore';
import { ExtensionW400 } from '@material-symbols-svg/react/icons/extension';
import { KeyboardArrowDownW400 } from '@material-symbols-svg/react/icons/keyboard-arrow-down';
import { FolderOffW400 } from '@material-symbols-svg/react/icons/folder-off';
import { FolderOpenW400 } from '@material-symbols-svg/react/icons/folder-open';
import { HandymanW400 } from '@material-symbols-svg/react/icons/handyman';
import { HelpW400 } from '@material-symbols-svg/react/icons/help';
import { ImageW400 } from '@material-symbols-svg/react/icons/image';
import { NewspaperW400 } from '@material-symbols-svg/react/icons/newspaper';
import { PaletteW400 } from '@material-symbols-svg/react/icons/palette';
import { PersonAddW400 } from '@material-symbols-svg/react/icons/person-add';
import { PlaylistRemoveW400 } from '@material-symbols-svg/react/icons/playlist-remove';
import { PriorityHighW400 } from '@material-symbols-svg/react/icons/priority-high';
import { RestartAltW400 } from '@material-symbols-svg/react/icons/restart-alt';
import { SearchW400 } from '@material-symbols-svg/react/icons/search';
import { SendW400 } from '@material-symbols-svg/react/icons/send';
import { SwapHorizW400 } from '@material-symbols-svg/react/icons/swap-horiz';
import { TagW400 } from '@material-symbols-svg/react/icons/tag';
import { TerminalW400 } from '@material-symbols-svg/react/icons/terminal';
import { VisibilityW400 } from '@material-symbols-svg/react/icons/visibility';

// Composer action icons
import { StopW400 } from '@material-symbols-svg/react/icons/stop';
import { QuickPhrasesW400 } from '@material-symbols-svg/react/icons/quick-phrases';

// Approval level icons
import { ShieldW400 } from '@material-symbols-svg/react/icons/shield';
import { BoltW400 } from '@material-symbols-svg/react/icons/bolt';
import { LockOpenW400 } from '@material-symbols-svg/react/icons/lock-open';

type SvgIcon = ComponentType<SVGProps<SVGSVGElement>>;

const ICON_MAP: Record<string, SvgIcon> = {
  approval: ApprovalW400,
  arrow_back: ArrowBackW400,
  attach_file: AttachFileW400,
  build: BuildW400,
  cable: CableW400,
  cancel: CancelW400,
  check_circle: CheckCircleW400,
  chevron_left: ChevronLeftW400,
  chevron_right: ChevronRightW400,
  close: CloseW400,
  code: CodeW400,
  delete: DeleteW400,
  edit: EditW400,
  display_settings: DisplaySettingsW400,
  draft: DraftW400,
  error: ErrorW400,
  folder: FolderW400,
  hub: HubW400,
  inbox: InboxW400,
  info: InfoW400,
  keyboard_return: KeyboardReturnW400,
  menu: MenuW400,
  menu_book: MenuBookW400,
  model_training: ModelTrainingW400,
  open_in_new: OpenInNewW400,
  pending: PendingW400,
  progress_activity: ProgressActivityW400,
  refresh: RefreshW400,
  schedule: ScheduleW400,
  settings: SettingsW400,
  settings_power: SettingsPowerW400,
  smart_toy: SmartToyW400,
  timer: TimerW400,
  tune: TuneW400,
  verified_user: VerifiedUserW400,
  warning: WarningW400,

  // Tool-specific icons
  bookmark: BookmarkW400,
  bookmarks: BookmarksW400,
  bookmark_remove: BookmarkRemoveW400,
  bug_report: BugReportW400,
  calculate: CalculateW400,
  calendar_month: CalendarMonthW400,
  chat_bubble: ChatBubbleW400,
  compress: CompressW400,
  data_usage: DataUsageW400,
  delete_sweep: DeleteSweepW400,
  description: DescriptionW400,
  difference: DifferenceW400,
  dns: DnsW400,
  edit_document: EditDocumentW400,
  engineering: EngineeringW400,
  event_busy: EventBusyW400,
  explore: ExploreW400,
  extension: ExtensionW400,
  expand_more: KeyboardArrowDownW400,
  folder_off: FolderOffW400,
  folder_open: FolderOpenW400,
  handyman: HandymanW400,
  help: HelpW400,
  image: ImageW400,
  newspaper: NewspaperW400,
  palette: PaletteW400,
  person_add: PersonAddW400,
  playlist_remove: PlaylistRemoveW400,
  priority_high: PriorityHighW400,
  restart_alt: RestartAltW400,
  search: SearchW400,
  send: SendW400,
  swap_horiz: SwapHorizW400,
  tag: TagW400,
  terminal: TerminalW400,
  visibility: VisibilityW400,

  // Composer action icons
  stop: StopW400,
  quick_phrases: QuickPhrasesW400,

  // Approval level icons
  shield: ShieldW400,
  bolt: BoltW400,
  lock_open: LockOpenW400,
};

interface IconProps {
  name: string;
  size?: number;
  className?: string;
  // [2026-06-02] Keep filled for source compatibility with existing callers.
  // Why: older font icons used font-variation settings for filled icons.
  // How: accept the prop but let the W400 outlined SVG component determine the shape.
  // Purpose: callers do not need a coordinated migration when the rendering backend changes.
  filled?: boolean;
}

export const Icon = ({ name, size = 20, className = '' }: IconProps) => {
  const SvgComponent = ICON_MAP[name];
  if (!SvgComponent) {
    // [2026-06-02] Preserve the previous text fallback for unknown icon names.
    // Why: dynamic tool data can provide icon strings before ICON_MAP is updated.
    // How: render readable text at the requested size instead of failing the component tree.
    // Purpose: the interface degrades visibly and keeps the missing icon easy to diagnose.
    return <span className={className} style={{ fontSize: size, lineHeight: 1, verticalAlign: 'middle' }}>{name}</span>;
  }

  return <SvgComponent className={className} width={size} height={size} style={{ verticalAlign: 'middle' }} />;
};

export type { IconProps };
