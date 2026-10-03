import * as AlertDialog from "@radix-ui/react-alert-dialog";
import * as Dialog from "@radix-ui/react-dialog";
import { useId, useRef, useState } from "react";
import type { EngineAsset, EngineKind, EngineWritePayload } from "../../api/types";
import { ApiError, errorMessage } from "../../api/client";
import { UnsavedChangesGuard } from "../../components/UnsavedChangesGuard";
import { normalizeSlug } from "../forms/slug";
import { blank, changedConfig, configOf, defaultVad, formOf, kindMeta, kinds, vadOf, vadParameters, validateForm } from "./model";
import type { EngineForm, FieldErrors } from "./model";

interface Props {
  engine: EngineAsset | null;
  initialKind: EngineKind;
  blocked: boolean;
  onSave: (payload: EngineWritePayload | Partial<EngineWritePayload>) => Promise<EngineAsset>;
  onSaved: (engine: EngineAsset, probe: boolean) => void;
  onClose: () => void;
  restoreFocus: () => void;
}
export function EngineEditor({ engine, initialKind, blocked, onSave, onSaved, onClose, restoreFocus }: Props) {
  const [baseline] = useState(() => engine ? configOf(engine) : blank(initialKind));
  const [form, setForm] = useState(() => formOf(baseline));
  const [slugEdited, setSlugEdited] = useState(Boolean(engine));
  const [errors, setErrors] = useState<FieldErrors>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const savingRef = useRef(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const prefix = useId();
  const dirty = JSON.stringify(form) !== JSON.stringify(formOf(baseline));
  const vad = vadOf(form.details);
  const set = <K extends keyof EngineForm>(key: K, value: EngineForm[K]) => {
    setForm(current => ({ ...current, [key]: value }));
    setErrors(current => Object.fromEntries(Object.entries(current).filter(([field]) => field !== key && !(key === "details" && field.startsWith("vad.")) && !((key === "api_key" || key === "credential_env") && (field === "api_key" || field === "credential_env")))));
    setFailure(null);
  };
  const focusErrors = () => requestAnimationFrame(() => contentRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus());
  const save = async (probe = false): Promise<boolean> => {
    if (savingRef.current || blocked) return false;
    const checked = validateForm(form);
    setErrors(checked.errors); setFailure(null);
    if (Object.keys(checked.errors).length) { setAdvanced(true); focusErrors(); return false; }
    savingRef.current = true; setSaving(true);
    try {
      const patch = changedConfig(baseline, checked.payload);
      const item = engine && !Object.keys(patch).length ? engine : await onSave(engine ? patch : checked.payload);
      onSaved(item, probe); return true;
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 422 && Array.isArray(reason.details)) {
        const fieldErrors: FieldErrors = {};
        for (const entry of reason.details) {
          if (entry && typeof entry === "object" && Array.isArray(entry.loc)) {
            const parts = entry.loc.filter((part: unknown) => part !== "body");
            const key = parts.includes("vad_postprocessing") ? `vad.${parts.at(-1)}` : parts.join(".");
            fieldErrors[key] = typeof entry.msg === "string" ? entry.msg : "请检查此字段。";
          }
        }
        setErrors(fieldErrors); setAdvanced(true); focusErrors();
      }
      setFailure(errorMessage(reason, "引擎保存失败，请检查配置后重试。")); return false;
    } finally { savingRef.current = false; setSaving(false); }
  };
  const close = () => { if (savingRef.current) return; if (dirty) setConfirmClose(true); else onClose(); };
  const fieldError = (key: string) => errors[key] && <small id={`${prefix}-${key}-error`} className="ec-field-error">{errors[key]}</small>;
  const attrs = (key: string) => ({ id: `${prefix}-${key}`, "aria-invalid": Boolean(errors[key]), "aria-describedby": errors[key] ? `${prefix}-${key}-error` : undefined });
  const changeKind = (kind: EngineKind) => {
    const meta = kindMeta[kind];
    setForm(current => ({ ...current, kind, api: meta.api, protocol: meta.protocol, target: current.protocol === "dummy" ? "" : current.target,
      api_key: kind === "llm" ? current.api_key : null, credential_env: kind === "llm" ? current.credential_env : null,
      request_timeout_seconds: kind === "llm" ? current.request_timeout_seconds || "180" : "" }));
    setErrors({}); setFailure(null);
  };
  return <>
    <UnsavedChangesGuard when={dirty} saving={saving || blocked} onSave={() => save()} onDiscard={onClose} />
    <Dialog.Root open onOpenChange={open => { if (!open) close(); }}>
      <Dialog.Portal><Dialog.Overlay className="drawer-backdrop" /><Dialog.Content ref={contentRef} className="ec-drawer" onCloseAutoFocus={event => { event.preventDefault(); restoreFocus(); }} aria-describedby={`${prefix}-description`}>
        <header><div><Dialog.Title>{engine ? `编辑 ${engine.name}` : "注册引擎"}</Dialog.Title><Dialog.Description id={`${prefix}-description`}>保存连接配置；健康状态由实际复检结果更新。</Dialog.Description></div><button className="icon-button" aria-label="关闭编辑引擎" disabled={saving} onClick={close}>×</button></header>
        <form className="ec-editor-form" onSubmit={event => { event.preventDefault(); void save(); }} noValidate>
          <div className="ec-drawer-body">
            {failure && <div className="alert alert-error" role="alert">{failure}</div>}
            <fieldset disabled={saving || blocked} className="ec-fieldset">
              <legend>基本信息</legend><div className="ec-form-grid">
                <label htmlFor={`${prefix}-kind`}>能力类型<select id={`${prefix}-kind`} disabled={Boolean(engine)} value={form.kind} onChange={event => changeKind(event.target.value as EngineKind)}>{kinds.map(kind => <option key={kind} value={kind}>{kindMeta[kind].label}</option>)}</select></label>
                <label htmlFor={`${prefix}-name`}>名称<input {...attrs("name")} maxLength={200} value={form.name} onChange={event => {
                  const name = event.target.value; set("name", name);
                  if (!engine && !slugEdited) set("slug", normalizeSlug(name));
                }} />{fieldError("name")}</label>
                <label className="ec-full" htmlFor={`${prefix}-slug`}>Slug<input {...attrs("slug")} maxLength={100} value={form.slug} onChange={event => { setSlugEdited(true); set("slug", event.target.value); }} />{fieldError("slug")}<small>小写字母、数字和连字符；中文名称请手动填写 Slug。</small></label>
                <label className="ec-full" htmlFor={`${prefix}-description-field`}>描述<textarea id={`${prefix}-description-field`} rows={2} value={form.description ?? ""} onChange={event => set("description", event.target.value || null)} /></label>
              </div>
            </fieldset>
            <fieldset disabled={saving || blocked} className="ec-fieldset"><legend>连接与凭据</legend><div className="ec-form-grid">
              {form.kind === "diarization" ? <label className="ec-full" htmlFor={`${prefix}-implementation`}>实现<select id={`${prefix}-implementation`} value={form.api === "dummy_diarize" ? "dummy" : "grpc"} onChange={event => {
                const dummy = event.target.value === "dummy";
                setForm(current => ({ ...current, api: dummy ? "dummy_diarize" : "ux_speaker_diarization", protocol: dummy ? "dummy" : "grpc", target: dummy ? "dummy://diarize" : "" }));
              }}><option value="grpc">外部 gRPC 引擎</option><option value="dummy">内置单说话人</option></select></label> : <p className="ec-full muted">接口：<code>{form.protocol.toUpperCase()} · {form.api}</code></p>}
              <label className="ec-full" htmlFor={`${prefix}-target`}>{form.protocol === "dummy" ? "内置地址" : form.kind === "llm" ? "API Base URL" : "gRPC 地址"}<input {...attrs("target")} maxLength={500} readOnly={form.protocol === "dummy"} value={form.target} onChange={event => set("target", event.target.value)} placeholder={form.kind === "llm" ? "https://api.example.com/v1" : "127.0.0.1:50051"} />{fieldError("target")}</label>
              {form.kind === "llm" && <>
                <label className="ec-full" htmlFor={`${prefix}-api_key`}>模型 API Key<input {...attrs("api_key")} value={form.api_key ?? ""} autoComplete="off" maxLength={8192} onChange={event => set("api_key", event.target.value || null)} />{fieldError("api_key")}<small>沿用当前服务的明文存储方式。</small></label>
                <label className="ec-full" htmlFor={`${prefix}-credential_env`}>服务进程环境变量名<input {...attrs("credential_env")} value={form.credential_env ?? ""} maxLength={200} onChange={event => set("credential_env", event.target.value || null)} />{fieldError("credential_env")}<small>与 API Key 至少填写一项；同时存在时直接 Key 优先。服务进程需要已设置该环境变量。</small></label>
              </>}
              {!engine && <label className="switch-control ec-full"><input type="checkbox" checked={form.enabled} onChange={event => set("enabled", event.target.checked)} /><span>启用并参与后台健康探测</span></label>}
              {engine && <p className="ec-full muted">{engine.enabled ? "已启用" : "已停用"}；启用或停用请在详情中操作。</p>}
            </div></fieldset>
            <details className="ec-advanced" open={advanced} onToggle={event => setAdvanced(event.currentTarget.open)}><summary>高级参数</summary>
              <fieldset disabled={saving || blocked} className="ec-fieldset"><div className="ec-form-grid">
                <label htmlFor={`${prefix}-connect_timeout_seconds`}>连接超时（秒）<input {...attrs("connect_timeout_seconds")} type="number" min={0} step="any" value={form.connect_timeout_seconds} onChange={event => set("connect_timeout_seconds", event.target.value)} />{fieldError("connect_timeout_seconds")}</label>
                <label htmlFor={`${prefix}-request_timeout_seconds`}>请求超时（秒）<input {...attrs("request_timeout_seconds")} type="number" min={0} step="any" value={form.request_timeout_seconds} onChange={event => set("request_timeout_seconds", event.target.value)} />{fieldError("request_timeout_seconds")}<small>留空使用服务默认值。</small></label>
              </div>
              {form.kind === "vad" && <section className="ec-vad"><h3>FireRed 后处理参数</h3><label className="switch-control"><input type="checkbox" checked={Boolean(vad)} onChange={event => {
                const details = { ...form.details }; delete details.vad_postprocessing;
                set("details", event.target.checked ? { ...details, vad_postprocessing: structuredClone(defaultVad) } : details);
              }} /><span>覆盖 VAD 服务端默认后处理</span></label>{vad && <div className="ec-form-grid">{vadParameters.map(([key, label]) => <label key={key} htmlFor={`${prefix}-vad.${key}`}>{label}<input {...attrs(`vad.${key}`)} type="number" min={key === "speech_threshold" ? 0 : 1} max={key === "speech_threshold" ? 1 : undefined} step={key === "speech_threshold" ? .01 : 1} value={Number.isFinite(vad[key]) ? vad[key] : ""} onChange={event => set("details", { ...form.details, vad_postprocessing: { ...(form.details?.vad_postprocessing as object), parameters: { ...vad, [key]: event.target.value === "" ? NaN : Number(event.target.value) } } })} />{fieldError(`vad.${key}`)}</label>)}</div>}</section>}
              </fieldset>
            </details>
          </div>
          <footer><span className="ec-save-state" role="status">{saving ? "正在保存…" : dirty ? "有未保存的修改" : ""}</span><button type="button" className="secondary-button" disabled={saving} onClick={close}>取消</button>{form.enabled && <button type="button" className="secondary-button" disabled={saving || blocked} onClick={() => void save(true)}>保存并复检</button>}<button className="primary-button" disabled={saving || blocked || Boolean(engine && !dirty)}>{saving ? "保存中…" : "保存引擎"}</button></footer>
        </form>
      </Dialog.Content></Dialog.Portal>
    </Dialog.Root>
    <AlertDialog.Root open={confirmClose} onOpenChange={setConfirmClose}><AlertDialog.Portal><AlertDialog.Overlay className="drawer-backdrop ec-confirm-overlay" /><AlertDialog.Content className="confirm-dialog ec-confirm"><AlertDialog.Title>还有未保存的修改</AlertDialog.Title><AlertDialog.Description>保存配置，或放弃这次修改后关闭。</AlertDialog.Description><div className="panel-actions"><AlertDialog.Cancel className="secondary-button">继续编辑</AlertDialog.Cancel><button className="danger-button" disabled={saving} onClick={onClose}>放弃修改</button><button className="primary-button" disabled={saving || blocked} onClick={async () => { setConfirmClose(false); await save(); }}>保存并关闭</button></div></AlertDialog.Content></AlertDialog.Portal></AlertDialog.Root>
  </>;
}
