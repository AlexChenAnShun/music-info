import React, { useEffect, useRef } from "react";
export function Button({ children, primary = false, ...props }) {
  return (
    <button
      type="button"
      className={"btn " + (primary ? "primary" : "outline")}
      {...props}
    >
      {children}
    </button>
  );
}
export function Field({ label, children }) {
  return (
    <label className="field">
      <span>{label}</span>
      {React.isValidElement(children) && typeof label === "string"
        ? React.cloneElement(children, {
            "aria-label": children.props["aria-label"] || label,
          })
        : children}
    </label>
  );
}
export function Alert({ children, error = false }) {
  return (
    <div
      className={"notice rx-alert" + (error ? " rx-error" : "")}
      role={error ? "alert" : "status"}
    >
      {children}
    </div>
  );
}
export function Modal({ title, children, onClose, actions, busy = false }) {
  const ref = useRef(null),
    close = useRef(onClose);
  close.current = onClose;
  const busyRef = useRef(busy);
  busyRef.current = busy;
  useEffect(() => {
    const previous = document.activeElement;
    const dialog = ref.current;
    dialog.showModal();
    const cancel = (e) => {
      e.preventDefault();
      if (!busyRef.current) close.current();
    };
    dialog.addEventListener("cancel", cancel);
    return () => {
      dialog.removeEventListener("cancel", cancel);
      dialog.close();
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  return (
    <dialog className="rx-dialog" ref={ref} aria-label={title}>
      <h3>{title}</h3>
      <div className="rx-dialog-body">{children}</div>
      <div className="dialog-actions">
        <Button disabled={busy} onClick={onClose}>
          取消
        </Button>
        {actions}
      </div>
    </dialog>
  );
}
