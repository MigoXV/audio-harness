import { WORKFLOW_PORT_FAMILIES, type WorkflowPortFamily } from "./workflowPortPresentation";

export function WorkflowPortMarker({ family }: { family: WorkflowPortFamily }) {
  const size = family === "control" ? 10.1213 : 8;
  const mask = `url(/workflow-ui/port-${family}.svg)`;
  return <span className="workflow-port-marker" aria-hidden="true" style={{ width: size, height: size, background: WORKFLOW_PORT_FAMILIES[family].color, maskImage: `${mask}, ${mask}`, maskSize: `100% 100%, ${family === "control" ? "58.08% 58.08%" : "62.5% 62.5%"}`, maskComposite: "exclude" }} />;
}
