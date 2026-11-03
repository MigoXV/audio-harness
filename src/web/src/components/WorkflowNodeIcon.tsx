export function WorkflowNodeIcon({ icon, small = false }: { icon: string; small?: boolean }) {
  return <span className={`workflow-node-icon${small ? " workflow-node-icon-small" : ""}`} aria-hidden="true">
    <span className="workflow-node-glyph" style={{ maskImage: `url(/workflow-icons/${small ? "library/" : ""}${icon}.svg)` }} />
  </span>;
}
