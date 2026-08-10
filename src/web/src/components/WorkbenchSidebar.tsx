import type { ReactNode } from "react";

interface Props {
  open: boolean;
  expandedTitle: string;
  collapsedTitle: string;
  ariaLabel: string;
  content: ReactNode;
  footer?: ReactNode;
  children?: ReactNode;
  onOpenChange: (open: boolean) => void;
}

export function WorkbenchSidebar({
  open,
  expandedTitle,
  collapsedTitle,
  ariaLabel,
  content,
  footer,
  children,
  onOpenChange,
}: Props) {
  return <aside className="workbench-sidebar panel" aria-label={ariaLabel}>
    <header>
      <h2>{open ? expandedTitle : collapsedTitle}</h2>
      <button
        className="icon-button"
        aria-label={open ? `收起${ariaLabel}` : `展开${ariaLabel}`}
        onClick={() => onOpenChange(!open)}
      >{open ? "‹" : "›"}</button>
    </header>
    {open && <div className="workbench-sidebar-body">
      {content}
      {children}
      {footer && <footer className="workbench-sidebar-footer">{footer}</footer>}
    </div>}
  </aside>;
}
