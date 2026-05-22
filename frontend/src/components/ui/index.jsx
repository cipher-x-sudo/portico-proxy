import React from 'react';
import './ui.css';

function MaterialIcon({ name, className = '', ariaHidden = true }) {
  if (!name) return null;
  return (
    <span className={`material-symbols-outlined ${className}`.trim()} aria-hidden={ariaHidden}>
      {name}
    </span>
  );
}

export function Button({
  variant = 'secondary',
  size = 'md',
  icon,
  children,
  className = '',
  type = 'button',
  ...props
}) {
  return (
    <button
      type={type}
      className={`ui-button ui-button--${variant} ui-button--${size} ${className}`.trim()}
      {...props}
    >
      <MaterialIcon name={icon} />
      {children && <span className="ui-button-label">{children}</span>}
    </button>
  );
}

export function IconButton({
  icon,
  label,
  variant = 'secondary',
  size = 'md',
  className = '',
  type = 'button',
  ...props
}) {
  return (
    <button
      type={type}
      className={`ui-icon-button ui-icon-button--${variant} ui-icon-button--${size} ${className}`.trim()}
      aria-label={label}
      title={props.title || label}
      {...props}
    >
      <MaterialIcon name={icon} ariaHidden />
    </button>
  );
}

export function Card({ as, className = '', children, ...props }) {
  const Element = as || 'section';
  return (
    <Element className={`ui-card ${className}`.trim()} {...props}>
      {children}
    </Element>
  );
}

export function Badge({ variant = 'neutral', className = '', children, ...props }) {
  return (
    <span className={`ui-badge ui-badge--${variant} ${className}`.trim()} {...props}>
      {children}
    </span>
  );
}

export function Toolbar({ className = '', children, ...props }) {
  return (
    <div className={`ui-toolbar ${className}`.trim()} {...props}>
      {children}
    </div>
  );
}

export function DataTable({ className = '', children, ...props }) {
  return (
    <div className="table-container">
      <table className={`data-table responsive-table ${className}`.trim()} {...props}>
        {children}
      </table>
    </div>
  );
}

export function FormField({ label, hint, children, className = '', ...props }) {
  return (
    <label className={`ui-form-field ${className}`.trim()} {...props}>
      {label && <span className="ui-form-label">{label}</span>}
      {children}
      {hint && <span className="ui-form-hint">{hint}</span>}
    </label>
  );
}

export function Modal({
  title,
  description,
  children,
  footer,
  onClose,
  size = 'md',
  className = '',
}) {
  return (
    <div className="ui-modal-overlay" onMouseDown={onClose}>
      <div
        className={`ui-modal-panel ui-modal-panel--${size} ${className}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="ui-modal-header">
          <div>
            {title && <h3 className="ui-modal-title">{title}</h3>}
            {description && <p className="ui-modal-description">{description}</p>}
          </div>
          {onClose && <IconButton icon="close" label="Close dialog" onClick={onClose} />}
        </div>
        <div className="ui-modal-body">{children}</div>
        {footer && <div className="ui-modal-footer">{footer}</div>}
      </div>
    </div>
  );
}
