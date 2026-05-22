import React, {
  useCallback,
  useMemo,
  useState,
} from 'react';
import { Button, IconButton, Modal } from './index.jsx';
import { ConfirmContext, ToastContext } from './feedback-hooks.js';

let nextToastId = 1;

function ToastIcon({ variant }) {
  const name =
    variant === 'success'
      ? 'check_circle'
      : variant === 'danger'
        ? 'error'
        : variant === 'warning'
          ? 'warning'
          : 'info';

  return (
    <span className="material-symbols-outlined" aria-hidden>
      {name}
    </span>
  );
}

export function UIProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const [confirmRequest, setConfirmRequest] = useState(null);

  const dismissToast = useCallback((id) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const toast = useCallback(({ title, message, variant = 'info', duration = 3200 } = {}) => {
    const id = nextToastId++;
    setToasts((current) => [...current, { id, title, message, variant }]);
    if (duration !== 0) {
      window.setTimeout(() => dismissToast(id), duration);
    }
    return id;
  }, [dismissToast]);

  const confirm = useCallback((options = {}) => {
    return new Promise((resolve) => {
      setConfirmRequest({
        title: options.title || 'Confirm action',
        message: options.message || 'Are you sure you want to continue?',
        confirmLabel: options.confirmLabel || 'Confirm',
        cancelLabel: options.cancelLabel || 'Cancel',
        variant: options.variant || 'danger',
        resolve,
      });
    });
  }, []);

  const closeConfirm = useCallback((accepted) => {
    setConfirmRequest((request) => {
      request?.resolve(Boolean(accepted));
      return null;
    });
  }, []);

  const toastValue = useMemo(() => toast, [toast]);
  const confirmValue = useMemo(() => confirm, [confirm]);

  return (
    <ToastContext.Provider value={toastValue}>
      <ConfirmContext.Provider value={confirmValue}>
        {children}
        <div className="ui-toast-stack" role="status" aria-live="polite">
          {toasts.map((item) => (
            <div key={item.id} className={`ui-toast ui-toast--${item.variant}`}>
              <div className="ui-toast-icon">
                <ToastIcon variant={item.variant} />
              </div>
              <div className="ui-toast-content">
                {item.title && <strong>{item.title}</strong>}
                {item.message && <p>{item.message}</p>}
              </div>
              <IconButton icon="close" label="Dismiss notification" size="sm" onClick={() => dismissToast(item.id)} />
            </div>
          ))}
        </div>
        {confirmRequest && (
          <Modal
            title={confirmRequest.title}
            description={confirmRequest.message}
            size="sm"
            onClose={() => closeConfirm(false)}
            footer={
              <>
                <Button variant="secondary" onClick={() => closeConfirm(false)}>
                  {confirmRequest.cancelLabel}
                </Button>
                <Button variant={confirmRequest.variant} onClick={() => closeConfirm(true)}>
                  {confirmRequest.confirmLabel}
                </Button>
              </>
            }
          />
        )}
      </ConfirmContext.Provider>
    </ToastContext.Provider>
  );
}
