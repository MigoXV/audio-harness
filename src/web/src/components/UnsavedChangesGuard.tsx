import { useCallback, useState } from "react";
import * as AlertDialog from "@radix-ui/react-alert-dialog";
import { useBeforeUnload, useBlocker } from "react-router-dom";

interface Props {
  when: boolean;
  saving: boolean;
  onSave: () => Promise<boolean>;
  onDiscard: () => void;
}

export function UnsavedChangesGuard({ when, saving, onSave, onDiscard }: Props) {
  const [saveFailed, setSaveFailed] = useState(false);
  const blocker = useBlocker(({ currentLocation, nextLocation }) =>
    when && (
      currentLocation.pathname !== nextLocation.pathname
      || currentLocation.search !== nextLocation.search
      || currentLocation.hash !== nextLocation.hash
    ),
  );

  useBeforeUnload(useCallback((event) => {
    if (!when) return;
    event.preventDefault();
    event.returnValue = "";
  }, [when]));

  const stay = () => {
    setSaveFailed(false);
    if (blocker.state === "blocked") blocker.reset();
  };
  const discard = () => {
    setSaveFailed(false);
    onDiscard();
    if (blocker.state === "blocked") blocker.proceed();
  };
  const saveAndLeave = async () => {
    setSaveFailed(false);
    const saved = await onSave();
    if (saved && blocker.state === "blocked") blocker.proceed();
    else if (!saved) setSaveFailed(true);
  };

  return <AlertDialog.Root open={blocker.state === "blocked"} onOpenChange={(open) => { if (!open && !saving) stay(); }}>
    <AlertDialog.Portal>
      <AlertDialog.Overlay className="drawer-backdrop" />
      <AlertDialog.Content className="confirm-dialog">
        <AlertDialog.Title>还有未保存的修改</AlertDialog.Title>
        <AlertDialog.Description>保存草稿后离开，或者明确放弃本次修改。</AlertDialog.Description>
        {saveFailed && <div className="alert alert-error">保存失败，已留在当前页面，请处理错误后重试。</div>}
        <div className="panel-actions">
          <button className="secondary-button" disabled={saving} onClick={stay}>继续编辑</button>
          <button className="danger-button" disabled={saving} onClick={discard}>放弃并离开</button>
          <button className="primary-button" disabled={saving} onClick={() => void saveAndLeave()}>{saving ? "正在保存…" : "保存并离开"}</button>
        </div>
      </AlertDialog.Content>
    </AlertDialog.Portal>
  </AlertDialog.Root>;
}
