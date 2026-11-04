import { useLayoutEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

import "./workspace-surfaces.css";

/** Native top-layer focus containment, inert background, and focus restoration. */
export function Modal({
  open,
  titleId,
  descriptionId,
  onClose,
  children,
}: {
  open: boolean;
  titleId: string;
  descriptionId?: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!open || !dialog) return;
    dialog.showModal();
    return () => dialog.close();
  }, [open]);

  if (!open) return null;

  return createPortal(
    <dialog
      ref={dialogRef}
      className="workspace-modal"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onKeyDown={(event) => {
        // An underlying editor or drawer must not also close on this Escape.
        if (event.key === "Escape") event.stopPropagation();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="workspace-modal-card">{children}</div>
    </dialog>,
    document.body,
  );
}
