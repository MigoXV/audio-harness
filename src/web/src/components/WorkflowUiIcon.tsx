const sizes = { search: 16, plus: 18, sliders: 18, check: 18, close: 28, chevron: 12 };

export function WorkflowUiIcon({ name }: { name: keyof typeof sizes }) {
  return <span className="workflow-ui-icon" aria-hidden="true" style={{ width: sizes[name], height: sizes[name], maskImage: `url(/workflow-ui/${name}.svg)` }} />;
}
