interface InlineFeedbackProps {
  title: string;
  message: string;
  tone?: "error" | "success";
  actionLabel?: string;
  onAction?: () => void;
}

export function InlineFeedback({
  title,
  message,
  tone = "error",
  actionLabel,
  onAction,
}: InlineFeedbackProps) {
  return (
    <div
      className={`inline-feedback inline-feedback-${tone}`}
      role={tone === "error" ? "alert" : "status"}
    >
      <div>
        <strong>{title}</strong>
        <span>{message}</span>
      </div>
      {actionLabel && onAction && (
        <button className="text-button" type="button" onClick={onAction}>
          {actionLabel}
        </button>
      )}
    </div>
  );
}
