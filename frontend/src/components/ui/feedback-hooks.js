import { createContext, useContext } from 'react';

export const ToastContext = createContext(null);
export const ConfirmContext = createContext(null);

export function useToast() {
  const toast = useContext(ToastContext);
  if (!toast) throw new Error('useToast must be used inside UIProvider');
  return toast;
}

export function useConfirm() {
  const confirm = useContext(ConfirmContext);
  if (!confirm) throw new Error('useConfirm must be used inside UIProvider');
  return confirm;
}
