import type { ReactNode } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { WorkflowNodeIcon } from "./WorkflowNodeIcon";
import { WorkflowPortMarker } from "./WorkflowPortMarker";
import { workflowPortPresentation } from "./workflowPortPresentation";
import { WorkflowUiIcon } from "./WorkflowUiIcon";

export interface InspectorPort {
  name: string;
  kind: string;
  label?: string;
  connections: string[];
}

interface Props {
  title: string;
  icon: string;
  nodeId: string;
  basic?: ReactNode;
  advanced?: ReactNode;
  inputs: InspectorPort[];
  outputs: InspectorPort[];
  diagnostics: ReactNode;
  portsActions?: ReactNode;
  onClose: () => void;
}

export function WorkflowInspector({ title, icon, nodeId, basic, advanced, inputs, outputs, diagnostics, portsActions, onClose }: Props) {
  return <aside className="audio-inspector panel workflow-inspector" aria-label="节点配置面板" onKeyDown={(event) => {
    if (event.key === "Escape") { event.stopPropagation(); onClose(); }
  }}>
    <header>
      <WorkflowNodeIcon icon={icon} />
      <h2>{title}</h2>
      {!nodeId.startsWith("$") && <code title={nodeId}>{nodeId}</code>}
      <button className="icon-button" aria-label="关闭节点配置" onClick={onClose}><WorkflowUiIcon name="close" /></button>
    </header>
    <Tabs.Root defaultValue={basic ? "basic" : "ports"} className="workflow-inspector-tabs">
      <Tabs.List aria-label="节点属性" className="workflow-inspector-tab-list">
        {basic && <Tabs.Trigger value="basic">基础配置</Tabs.Trigger>}
        <Tabs.Trigger value="ports">输入与输出</Tabs.Trigger>
        {advanced && <Tabs.Trigger value="advanced">高级设置</Tabs.Trigger>}
      </Tabs.List>
      {basic && <Tabs.Content value="basic" className="audio-inspector-body">{basic}{diagnostics}</Tabs.Content>}
      <Tabs.Content value="ports" className="audio-inspector-body workflow-inspector-ports">
        {([["输入", inputs], ["输出", outputs]] as const).filter(([, ports]) => ports.length > 0).map(([direction, ports]) => <section key={direction} aria-label={`节点${direction}`}>
          <h3>{direction}</h3>
          <ul>{ports.map((port) => <li key={port.name} title={`${port.name}: ${port.kind}`}>
            <WorkflowPortMarker family={workflowPortPresentation(port.kind).family} />
            <div><span>{port.label ?? workflowPortPresentation(port.kind).label}</span>
              <small>{port.connections.length ? `${direction === "输入" ? "来自" : "连接至"} ${port.connections.join("、")}` : "未连接"}</small>
            </div>
          </li>)}</ul>
        </section>)}
        {inputs.length + outputs.length === 0 && <p>此节点没有已声明的端口。</p>}
        {portsActions}
        {diagnostics}
      </Tabs.Content>
      {advanced && <Tabs.Content value="advanced" className="audio-inspector-body">{advanced}{diagnostics}</Tabs.Content>}
    </Tabs.Root>
  </aside>;
}
