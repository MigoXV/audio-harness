import { useEffect, useMemo, useState } from "react";

import { errorMessage } from "../api/client";
import type {
  AudioHarnessApi,
  CapacitySettings,
  CapacitySettingsUpdate,
  WorkflowSettings,
  WorkflowSettingsUpdate,
} from "../api/types";

interface RuntimeSettingsPanelProps {
  api: AudioHarnessApi;
  refreshVersion: number;
}

interface NumericField {
  key: Exclude<
    keyof WorkflowSettingsUpdate,
    "default_language_code" | "revision"
  >;
  label: string;
  unit: string;
  min: number;
  step: number;
  hint: string;
}

const AUDIO_FIELDS: NumericField[] = [
  {
    key: "min_chunk_bytes",
    label: "输入重分块",
    unit: "bytes",
    min: 2,
    step: 2,
    hint: "必须为偶数，保持 PCM S16LE 对齐。",
  },
  {
    key: "min_segment_seconds",
    label: "最短语音段",
    unit: "s",
    min: 0,
    step: 0.1,
    hint: "短于该时长的 VAD 片段不会进入 ASR。",
  },
  {
    key: "max_segment_seconds",
    label: "最长语音段",
    unit: "s",
    min: 0.1,
    step: 1,
    hint: "超长发言按该窗口切分，避免单次识别过大。",
  },
  {
    key: "audio_buffer_seconds",
    label: "轮转音频缓冲",
    unit: "s",
    min: 0.1,
    step: 1,
    hint: "必须覆盖最长语音段和起点前置音频。",
  },
  {
    key: "speech_pre_roll_seconds",
    label: "语音起点前置",
    unit: "s",
    min: 0,
    step: 0.1,
    hint: "为 VAD 起点补回前文，避免片段开头缺字。",
  },
];

const QUEUE_FIELDS: NumericField[] = [
  {
    key: "audio_queue_capacity",
    label: "音频队列",
    unit: "chunks",
    min: 1,
    step: 1,
    hint: "限制每个会话尚未完成 VAD 的音频块。",
  },
  {
    key: "segment_queue_capacity",
    label: "片段队列",
    unit: "segments",
    min: 1,
    step: 1,
    hint: "限制等待 ASR 的 VAD 片段数量。",
  },
  {
    key: "result_queue_capacity",
    label: "结果队列",
    unit: "events",
    min: 1,
    step: 1,
    hint: "限制尚未写回客户端的识别结果。",
  },
];

function FieldRow({
  field,
  value,
  disabled,
  onChange,
}: {
  field: NumericField;
  value: number;
  disabled: boolean;
  onChange: (key: NumericField["key"], value: number) => void;
}) {
  return (
    <label className="settings-row">
      <span className="settings-copy">
        <strong>{field.label}</strong>
        <small>{field.hint}</small>
      </span>
      <span className="number-control">
        <input
          aria-label={field.label}
          type="number"
          min={field.min}
          step={field.step}
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(field.key, Number(event.target.value))}
        />
        <span>{field.unit}</span>
      </span>
    </label>
  );
}

function CapacityField({
  label,
  hint,
  value,
  unit,
  disabled,
  onChange,
}: {
  label: string;
  hint: string;
  value: number;
  unit: string;
  disabled: boolean;
  onChange: (value: number) => void;
}) {
  return (
    <label className="settings-row">
      <span className="settings-copy">
        <strong>{label}</strong>
        <small>{hint}</small>
      </span>
      <span className="number-control">
        <input
          aria-label={label}
          type="number"
          min={1}
          step={1}
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(Number(event.target.value))}
        />
        <span>{unit}</span>
      </span>
    </label>
  );
}

export function RuntimeSettingsPanel({
  api,
  refreshVersion,
}: RuntimeSettingsPanelProps) {
  const [savedWorkflow, setSavedWorkflow] =
    useState<WorkflowSettings | null>(null);
  const [workflow, setWorkflow] = useState<WorkflowSettings | null>(null);
  const [savedCapacity, setSavedCapacity] =
    useState<CapacitySettings | null>(null);
  const [capacity, setCapacity] = useState<CapacitySettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [workflowSaving, setWorkflowSaving] = useState(false);
  const [capacitySaving, setCapacitySaving] = useState(false);
  const [workflowError, setWorkflowError] = useState<string | null>(null);
  const [capacityError, setCapacityError] = useState<string | null>(null);
  const [workflowNotice, setWorkflowNotice] = useState<string | null>(null);
  const [capacityNotice, setCapacityNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.allSettled([
      api.getWorkflowSettings(controller.signal).then((settings) => {
        setWorkflowError(null);
        setSavedWorkflow(settings);
        setWorkflow(settings);
      }),
      api.getCapacitySettings(controller.signal).then((settings) => {
        setCapacityError(null);
        setSavedCapacity(settings);
        setCapacity(settings);
      }),
    ]).then((results) => {
      if (controller.signal.aborted) {
        return;
      }
      const [workflowResult, capacityResult] = results;
      if (workflowResult.status === "rejected") {
        setWorkflowError(errorMessage(workflowResult.reason));
      }
      if (capacityResult.status === "rejected") {
        setCapacityError(errorMessage(capacityResult.reason));
      }
      setLoading(false);
    });
    return () => controller.abort();
  }, [api, refreshVersion]);

  const workflowDirty = useMemo(
    () =>
      Boolean(
        savedWorkflow &&
          workflow &&
          JSON.stringify(savedWorkflow) !== JSON.stringify(workflow),
      ),
    [savedWorkflow, workflow],
  );
  const capacityDirty = useMemo(
    () =>
      Boolean(
        savedCapacity &&
          capacity &&
          JSON.stringify(savedCapacity) !== JSON.stringify(capacity),
      ),
    [capacity, savedCapacity],
  );

  const updateWorkflowNumber = (
    key: NumericField["key"],
    value: number,
  ) => {
    setWorkflow((current) =>
      current ? { ...current, [key]: value } : current,
    );
    setWorkflowNotice(null);
  };

  const saveWorkflow = async () => {
    if (!workflow) {
      return;
    }
    setWorkflowSaving(true);
    setWorkflowError(null);
    setWorkflowNotice(null);
    try {
      const payload: WorkflowSettingsUpdate = {
        min_chunk_bytes: workflow.min_chunk_bytes,
        min_segment_seconds: workflow.min_segment_seconds,
        max_segment_seconds: workflow.max_segment_seconds,
        audio_buffer_seconds: workflow.audio_buffer_seconds,
        speech_pre_roll_seconds: workflow.speech_pre_roll_seconds,
        default_language_code: workflow.default_language_code,
        audio_queue_capacity: workflow.audio_queue_capacity,
        segment_queue_capacity: workflow.segment_queue_capacity,
        result_queue_capacity: workflow.result_queue_capacity,
        revision: workflow.revision,
      };
      const next = await api.updateWorkflowSettings(payload);
      setSavedWorkflow(next);
      setWorkflow(next);
      setWorkflowNotice("处理策略已保存；新会话立即使用这份配置。");
    } catch (reason) {
      setWorkflowError(errorMessage(reason));
    } finally {
      setWorkflowSaving(false);
    }
  };

  const saveCapacity = async () => {
    if (!capacity) {
      return;
    }
    setCapacitySaving(true);
    setCapacityError(null);
    setCapacityNotice(null);
    try {
      const payload: CapacitySettingsUpdate = {
        grpc: { max_sessions: capacity.grpc.max_sessions },
        realtime: { max_sessions: capacity.realtime.max_sessions },
        webrtc: { max_sessions: capacity.webrtc.max_sessions },
        max_asr_concurrency: capacity.max_asr_concurrency,
        revision: capacity.revision,
      };
      const next = await api.updateCapacitySettings(payload);
      setSavedCapacity(next);
      setCapacity(next);
      setCapacityNotice(
        "容量策略已保存；新会话和后续 ASR 任务立即使用新额度。",
      );
    } catch (reason) {
      setCapacityError(errorMessage(reason));
    } finally {
      setCapacitySaving(false);
    }
  };

  const renderWorkflowSection = (
    title: string,
    description: string,
    fields: NumericField[],
  ) => (
    <section className="settings-section">
      <header>
        <h2>{title}</h2>
        <p>{description}</p>
      </header>
      <div className="settings-rows">
        {fields.map((field) => (
          <FieldRow
            key={field.key}
            field={field}
            value={workflow?.[field.key] ?? 0}
            disabled={workflowSaving || !workflow}
            onChange={updateWorkflowNumber}
          />
        ))}
      </div>
    </section>
  );

  if (loading && !workflow && !capacity) {
    return <main className="settings-workspace quiet-state">读取运行设置…</main>;
  }

  return (
    <main className="settings-workspace">
      <header className="settings-heading">
        <div>
          <h1>运行设置</h1>
          <p>
            配置新会话使用的音频处理参数，以及各协议和共享推理的容量边界。
          </p>
        </div>
        <dl className="settings-meta">
          <div>
            <dt>处理 revision</dt>
            <dd>{workflow?.revision ?? "不可用"}</dd>
          </div>
          <div>
            <dt>容量 revision</dt>
            <dd>{capacity?.revision ?? "不可用"}</dd>
          </div>
        </dl>
      </header>

      <div className="settings-policy-heading">
        <div>
          <h2>处理参数</h2>
        </div>
        {workflow && (
          <span>
            更新于 {new Date(workflow.updated_at).toLocaleString("zh-CN")}
          </span>
        )}
      </div>

      {workflowError && (
        <div className="inline-error settings-feedback">{workflowError}</div>
      )}
      {workflowNotice && (
        <div className="settings-feedback settings-success" role="status">
          {workflowNotice}
        </div>
      )}

      {workflow ? (
        <>
          {renderWorkflowSection(
            "音频与分段",
            "决定 PCM 重分块、VAD 片段边界与轮转缓冲的保留范围。",
            AUDIO_FIELDS,
          )}

          <section className="settings-section">
            <header>
              <h2>默认识别</h2>
              <p>客户端未声明语言或使用 auto 时采用该值。</p>
            </header>
            <div className="settings-rows">
              <label className="settings-row">
                <span className="settings-copy">
                  <strong>默认语言代码</strong>
                  <small>使用 BCP 47，例如 zh-CN、en-US。</small>
                </span>
                <input
                  className="language-input"
                  aria-label="默认语言代码"
                  value={workflow.default_language_code}
                  disabled={workflowSaving}
                  onChange={(event) => {
                    setWorkflow({
                      ...workflow,
                      default_language_code: event.target.value,
                    });
                    setWorkflowNotice(null);
                  }}
                />
              </label>
            </div>
          </section>

          {renderWorkflowSection(
            "队列与背压",
            "队列达到上限后通过异步背压限制单个会话的内存增长。",
            QUEUE_FIELDS,
          )}
          <footer className="settings-policy-actions">
            <span>
              {workflowDirty ? "处理策略有尚未保存的修改" : "处理策略已同步"}
            </span>
            <div>
              <button
                className="button button-secondary"
                type="button"
                disabled={!workflowDirty || workflowSaving}
                onClick={() => {
                  setWorkflow(savedWorkflow);
                  setWorkflowError(null);
                  setWorkflowNotice(null);
                }}
              >
                恢复处理策略
              </button>
              <button
                className="button button-primary"
                type="button"
                disabled={!workflowDirty || workflowSaving}
                onClick={() => void saveWorkflow()}
              >
                {workflowSaving ? "保存中…" : "保存处理策略"}
              </button>
            </div>
          </footer>
        </>
      ) : (
        <div className="quiet-state">处理策略当前不可用。</div>
      )}

      <div className="settings-policy-heading">
        <div>
          <h2>容量设置</h2>
        </div>
        {capacity && (
          <span>
            更新于 {new Date(capacity.updated_at).toLocaleString("zh-CN")}
          </span>
        )}
      </div>

      {capacityError && (
        <div className="inline-error settings-feedback">{capacityError}</div>
      )}
      {capacityNotice && (
        <div className="settings-feedback settings-success" role="status">
          {capacityNotice}
        </div>
      )}

      {capacity ? (
        <>
          <section className="settings-section">
            <header>
              <h2>协议会话</h2>
              <p>各入口额度相互独立，不会占用其他协议的会话额度。</p>
            </header>
            <div className="settings-rows">
              <CapacityField
                label="gRPC 最大会话"
                hint="限制 50051 上同时运行的 StreamingRecognize 调用。"
                value={capacity.grpc.max_sessions}
                unit="sessions"
                disabled={capacitySaving}
                onChange={(value) => {
                  setCapacity({
                    ...capacity,
                    grpc: { max_sessions: value },
                  });
                  setCapacityNotice(null);
                }}
              />
              <CapacityField
                label="Realtime 最大会话"
                hint="限制 8000 上使用 WebSocket 的 Realtime 会话。"
                value={capacity.realtime.max_sessions}
                unit="sessions"
                disabled={capacitySaving}
                onChange={(value) => {
                  setCapacity({
                    ...capacity,
                    realtime: { max_sessions: value },
                  });
                  setCapacityNotice(null);
                }}
              />
              <CapacityField
                label="WebRTC 最大会话"
                hint="限制 8000 上通过 /v1/realtime/calls 建立的 PeerConnection。"
                value={capacity.webrtc.max_sessions}
                unit="sessions"
                disabled={capacitySaving}
                onChange={(value) => {
                  setCapacity({
                    ...capacity,
                    webrtc: { max_sessions: value },
                  });
                  setCapacityNotice(null);
                }}
              />
            </div>
          </section>

          <section className="settings-section">
            <header>
              <h2>共享推理</h2>
              <p>统一保护当前绑定的 ASR 引擎，避免多协议叠加压垮下游。</p>
            </header>
            <div className="settings-rows">
              <CapacityField
                label="最大 ASR 并发"
                hint="降低额度不会取消已经进入 ASR 的任务。"
                value={capacity.max_asr_concurrency}
                unit="jobs"
                disabled={capacitySaving}
                onChange={(value) => {
                  setCapacity({ ...capacity, max_asr_concurrency: value });
                  setCapacityNotice(null);
                }}
              />
            </div>
          </section>
          <footer className="settings-policy-actions">
            <span>
              {capacityDirty ? "容量策略有尚未保存的修改" : "容量策略已同步"}
            </span>
            <div>
              <button
                className="button button-secondary"
                type="button"
                disabled={!capacityDirty || capacitySaving}
                onClick={() => {
                  setCapacity(savedCapacity);
                  setCapacityError(null);
                  setCapacityNotice(null);
                }}
              >
                恢复容量策略
              </button>
              <button
                className="button button-primary"
                type="button"
                disabled={!capacityDirty || capacitySaving}
                onClick={() => void saveCapacity()}
              >
                {capacitySaving ? "保存中…" : "保存容量策略"}
              </button>
            </div>
          </footer>
        </>
      ) : (
        <div className="quiet-state">容量策略当前不可用。</div>
      )}
    </main>
  );
}
