import { useEffect, useMemo, useState } from "react";

import { errorMessage } from "../api/client";
import type {
  AudioHarnessApi,
  CapacitySettings,
  CapacitySettingsUpdate,
} from "../api/types";

interface CapacitySettingsPanelProps {
  api: AudioHarnessApi;
  refreshVersion: number;
}

export function CapacitySettingsPanel({
  api,
  refreshVersion,
}: CapacitySettingsPanelProps) {
  const [saved, setSaved] = useState<CapacitySettings | null>(null);
  const [draft, setDraft] = useState<CapacitySettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void api
      .getCapacitySettings(controller.signal)
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

  const save = async () => {
    if (!draft) {
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const payload: CapacitySettingsUpdate = {
        grpc: { max_sessions: draft.grpc.max_sessions },
        realtime: { max_sessions: draft.realtime.max_sessions },
        webrtc: { max_sessions: draft.webrtc.max_sessions },
        max_asr_concurrency: draft.max_asr_concurrency,
        revision: draft.revision,
      };
      const next = await api.updateCapacitySettings(payload);
      setSaved(next);
      setDraft(next);
      setNotice("容量设置已保存；新会话和后续 ASR 任务立即使用新额度。");
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  };

  if (loading && !draft) {
    return <main className="settings-workspace quiet-state">读取容量设置…</main>;
  }

  if (!draft) {
    return (
      <main className="settings-workspace">
        <div className="inline-error">{error ?? "无法读取容量设置"}</div>
      </main>
    );
  }

  const field = (
    label: string,
    hint: string,
    value: number,
    unit: string,
    onChange: (value: number) => void,
  ) => (
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
          disabled={saving}
          onChange={(event) => onChange(Number(event.target.value))}
        />
        <span>{unit}</span>
      </span>
    </label>
  );

  return (
    <main className="settings-workspace">
      <header className="settings-heading">
        <div>
          <span className="eyebrow">流量治理</span>
          <h1>容量设置</h1>
          <p>协议会话额度彼此隔离，ASR 并发额度由所有协议共享。</p>
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

      <section className="settings-section">
        <header>
          <h2>协议会话</h2>
          <p>一个入口达到上限不会占用或阻断另一种协议的会话额度。</p>
        </header>
        <div className="settings-rows">
          {field(
            "gRPC 最大会话",
            "限制 50051 上同时运行的 StreamingRecognize 调用。",
            draft.grpc.max_sessions,
            "sessions",
            (value) =>
              setDraft({ ...draft, grpc: { max_sessions: value } }),
          )}
          {field(
            "Realtime 最大会话",
            "限制 8000 上使用 WebSocket 的 Realtime 会话。",
            draft.realtime.max_sessions,
            "sessions",
            (value) =>
              setDraft({ ...draft, realtime: { max_sessions: value } }),
          )}
          {field(
            "WebRTC 最大会话",
            "限制 8000 上通过 /v1/realtime/calls 建立的 PeerConnection。",
            draft.webrtc.max_sessions,
            "sessions",
            (value) =>
              setDraft({ ...draft, webrtc: { max_sessions: value } }),
          )}
        </div>
      </section>

      <section className="settings-section">
        <header>
          <h2>共享推理</h2>
          <p>统一保护当前绑定的 ASR 引擎，避免多协议叠加压垮下游。</p>
        </header>
        <div className="settings-rows">
          {field(
            "最大 ASR 并发",
            "降低额度不会取消已经进入 ASR 的任务。",
            draft.max_asr_concurrency,
            "jobs",
            (value) =>
              setDraft({ ...draft, max_asr_concurrency: value }),
          )}
        </div>
      </section>

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
