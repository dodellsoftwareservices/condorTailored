import { AlertTriangle, Loader2 } from "lucide-react";
import { useId, type ReactNode } from "react";

import { Modal } from "@/components/ui/Modal";

/**
 * Shared destructive-confirmation modal for agent/strategy delete flows.
 *
 * Renders the overlay + card + Cancel + destructive button shell that was
 * previously copy-pasted across Agents.tsx and AgentDetail.tsx (ARCH-059).
 * The destructive button uses the themed `bg-[var(--color-red)]` token so it
 * honors the colorblind theme's red remap (index.css), matching the editor
 * dialogs (EditorDialogs.tsx) instead of a hardcoded `bg-red-500`.
 *
 * The native modal contains focus, restores the opener, and handles Escape
 * without also closing an underlying editor or drawer.
 */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel = "Delete",
  pendingLabel = "Deleting...",
  isPending = false,
  isError = false,
  errorText,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel?: string;
  pendingLabel?: string;
  isPending?: boolean;
  isError?: boolean;
  errorText?: string;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const titleId = useId();
  const descriptionId = useId();

  return (
    <Modal open={open} onClose={onClose} titleId={titleId} descriptionId={descriptionId}>
      <div className="workspace-dialog-icon" aria-hidden="true"><AlertTriangle size={22} /></div>
      <h2 id={titleId} className="workspace-modal-title">{title}</h2>
      <div id={descriptionId} className="workspace-modal-description">{children}</div>
      <div className="workspace-modal-actions">
        <button onClick={onClose} className="workspace-button">Cancel</button>
        <button
          onClick={onConfirm}
          disabled={isPending}
          className="workspace-button workspace-button-danger"
          aria-busy={isPending}
        >
          {isPending && <Loader2 size={14} className="animate-spin" aria-hidden="true" />}
          {isPending ? pendingLabel : confirmLabel}
        </button>
      </div>
      {isError && errorText && (
        <p role="alert" className="workspace-dialog-error">{errorText}</p>
      )}
    </Modal>
  );
}
