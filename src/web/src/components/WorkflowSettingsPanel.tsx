import { useEffect, useMemo, useState } from "react";

import { errorMessage } from "../api/client";
import type {
  AudioHarnessApi,
  WorkflowSettings,
  WorkflowSettingsUpdate,
} from "../api/types";

interface WorkflowSettingsPanelProps {
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

export function WorkflowSettingsPanel({
  api,
  refreshVersion,
}: WorkflowSettingsPanelProps) {
  const [saved, setSaved] = useState<WorkflowSettings | null>(null);
  const [draft, setDraft] = useState<WorkflowSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void api
      .getWorkflowSettings(controller.signal)
      .then((settings) => {
        setSaved(settings);
        setDraft(settings);
      })
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setError(errorMessage(reason));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [api, refreshVersion]);

  const dirty = useMemo(
    () => Boolean(saved && draft && JSON.stringify(saved) !== JSON.stringify(draft)),
    [draft, saved],
  );

  const updateNumber = (key: NumericField["key"], value: number) => {
    setDraft((current) => (current ? { ...current, [key]: value } : current));
    setNotice(null);
  };

  const save = async () => {
    if (!draft) {
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const payload: WorkflowSettingsUpdate = {
        min_chunk_bytes: draft.min_chunk_bytes,
        min_segment_seconds: draft.min_segment_seconds,
        max_segment_seconds: draft.max_segment_seconds,
        audio_buffer_seconds: draft.audio_buffer_seconds,
        speech_pre_roll_seconds: draft.speech_pre_roll_seconds,
        default_language_code: draft.default_language_code,
        audio_queue_capacity: draft.audio_queue_capacity,
        segment_queue_capacity: draft.segment_queue_capacity,
        result_queue_capacity: draft.result_queue_capacity,
        revision: draft.revision,
      };
      const next = await api.updateWorkflowSettings(payload);
      setSaved(next);
      setDraft(next);
      setNotice("工作流设置已保存；新会话立即使用这份配置。");
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  };

  if (loading && !draft) {
    return <main className="settings-workspace quiet-state">读取工作流设置…</main>;
  }

  if (!draft) {
    return (
      <main className="settings-workspace">
        <div className="inline-error">{error ?? "无法读取工作流设置"}</div>
      </main>
    );
  }

  const renderSection = (
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
            value={draft[field.key]}
            disabled={saving}
            onChange={updateNumber}
          />
        ))}
      </div>
    </section>
  );

  return (
    <main className="settings-workspace">
      <header className="settings-heading">
        <div>
          <span className="eyebrow">运行策略</span>
          <h1>工作流设置</h1>
          <p>配置按会话快照生效；已开始的音频流不会在处理中途切换。</p>
        </div>
        <dl className="settings-meta">
          <div>
            <dt>revision</dt>
            <dd>{draft.revision}</dd>
          </div>
          <div>
            <dt>更新时间</dt>
            <dd>{new Date(draft.updated_at).toLocaleString("zh-CN")}</dd>
          </div>
        </dl>
      </header>

      {error && <div className="inline-error settings-feedback">{error}</div>}
      {notice && (
        <div className="settings-feedback settings-success" role="status">
          {notice}
        </div>
      )}

      {renderSection(
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
              value={draft.default_language_code}
              disabled={saving}
              onChange={(event) => {
                setDraft({ ...draft, default_language_code: event.target.value });
                setNotice(null);
              }}
            />
          </label>
        </div>
      </section>

      {renderSection(
        "有界队列",
        "队列达到上限后通过异步背压限制内存增长。",
        QUEUE_FIELDS,
      )}
      <footer className="settings-actions">
        <span>{dirty ? "有尚未保存的修改" : "当前设置已同步"}</span>
        <div>
          <button
            className="button button-secondary"
            type="button"
            disabled={!dirty || saving}
            onClick={() => {
              setDraft(saved);
              setError(null);
              setNotice(null);
            }}
          >
            恢复已保存
          </button>
          <button
            className="button button-primary"
            type="button"
            disabled={!dirty || saving}
            onClick={() => void save()}
          >
            {saving ? "保存中…" : "保存设置"}
          </button>
        </div>
      </footer>
    </main>
  );
}
