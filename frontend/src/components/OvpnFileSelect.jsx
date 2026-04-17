import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  filterOvpnFilesByQuery,
  formatOvpnDisplayLabel,
  formatOvpnRichLabel,
  parseProtonOvpnMeta,
  parseUnitedStatesOvpnMeta,
} from '../utils/ovpnFiles';
import './OvpnFileSelect.css';

function MenuItem({ file, selected, onPick }) {
  const proton = parseProtonOvpnMeta(file);
  const us = parseUnitedStatesOvpnMeta(file);
  const isSelected = file === selected;

  return (
    <li>
      <button
        type="button"
        className={`ovpn-file-select-item${isSelected ? ' is-selected' : ''}`}
        onClick={() => onPick(file)}
      >
        <div className="ovpn-file-select-item-inner">
          {proton ? (
            <>
              <span className="ovpn-file-select-id">{proton.id}</span>
              <div className="ovpn-file-select-meta">
                <span className="ovpn-file-select-loc">
                  {proton.country} · {proton.region}
                </span>
                <span className="ovpn-file-select-proto">{proton.proto}</span>
              </div>
            </>
          ) : us ? (
            <div className="ovpn-file-select-meta">
              <span className="ovpn-file-select-loc">
                {us.state} · {us.city}
              </span>
            </div>
          ) : (
            <span className="ovpn-file-select-fallback">{formatOvpnDisplayLabel(file) || file}</span>
          )}
        </div>
      </button>
    </li>
  );
}

export default function OvpnFileSelect({
  files,
  value,
  onChange,
  disabled,
  placeholder = 'Select profile…',
}) {
  const [open, setOpen] = useState(false);
  const [menuStyle, setMenuStyle] = useState(null);
  const [filterQuery, setFilterQuery] = useState('');
  const triggerRef = useRef(null);
  const searchInputRef = useRef(null);

  const close = useCallback(() => {
    setFilterQuery('');
    setOpen(false);
  }, []);

  const filteredFiles = useMemo(
    () => filterOvpnFilesByQuery(files, filterQuery),
    [files, filterQuery]
  );

  const updatePosition = useCallback(() => {
    const el = triggerRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const gap = 6;
    const maxH = Math.min(window.innerHeight * 0.55, 22 * 16);
    let top = r.bottom + gap;
    if (top + maxH > window.innerHeight - 8) {
      top = Math.max(8, r.top - gap - maxH);
    }
    const width = Math.max(r.width, 220);
    let left = r.left;
    if (left + width > window.innerWidth - 8) {
      left = Math.max(8, window.innerWidth - 8 - width);
    }
    setMenuStyle({
      top,
      left,
      width,
      maxHeight: maxH,
    });
  }, []);

  useLayoutEffect(() => {
    if (!open) return;
    updatePosition();
  }, [open, updatePosition, files.length]);

  useEffect(() => {
    if (!open) return;
    const onScroll = () => updatePosition();
    const onResize = () => updatePosition();
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onResize);
    };
  }, [open, updatePosition]);

  useEffect(() => {
    if (!open) return;
    const id = requestAnimationFrame(() => {
      searchInputRef.current?.focus();
      searchInputRef.current?.select?.();
    });
    return () => cancelAnimationFrame(id);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key === 'Escape') close();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, close]);

  const displayLabel = value ? formatOvpnRichLabel(value) : '';

  const onPick = (file) => {
    onChange(file);
    close();
  };

  const menu =
    open &&
    menuStyle &&
    createPortal(
      <>
        <div className="ovpn-file-select-backdrop" aria-hidden onClick={close} />
        <div
          className="ovpn-file-select-panel"
          style={{
            top: menuStyle.top,
            left: menuStyle.left,
            width: menuStyle.width,
            maxHeight: menuStyle.maxHeight,
          }}
          onMouseDown={(e) => e.preventDefault()}
          role="dialog"
          aria-label="Choose OpenVPN profile"
        >
          <div className="ovpn-file-select-search-row">
            <span className="material-symbols-outlined" aria-hidden>
              search
            </span>
            <input
              ref={searchInputRef}
              type="search"
              className="ovpn-file-select-search-input"
              placeholder="Search state, city, ID, region, filename…"
              value={filterQuery}
              onChange={(e) => setFilterQuery(e.target.value)}
              aria-label="Filter profiles"
              autoComplete="off"
              spellCheck={false}
            />
          </div>
          <ul className="ovpn-file-select-list" aria-label="OpenVPN profiles">
            <li>
              <button
                type="button"
                className={`ovpn-file-select-item${!value ? ' is-selected' : ''}`}
                onClick={() => onPick('')}
              >
                <div className="ovpn-file-select-item-inner">
                  <span className="ovpn-file-select-placeholder">{placeholder}</span>
                </div>
              </button>
            </li>
            {files.length === 0 ? (
              <li className="ovpn-file-select-empty">No profiles available</li>
            ) : filteredFiles.length === 0 ? (
              <li className="ovpn-file-select-empty">No matching profiles</li>
            ) : (
              filteredFiles.map((file) => (
                <MenuItem key={file} file={file} selected={value} onPick={onPick} />
              ))
            )}
          </ul>
        </div>
      </>,
      document.body
    );

  return (
    <div className="ovpn-file-select">
      <button
        ref={triggerRef}
        type="button"
        className="ovpn-file-select-trigger"
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          if (disabled) return;
          if (open) close();
          else setOpen(true);
        }}
      >
        <span className="ovpn-file-select-trigger-label">
          {value ? displayLabel : placeholder}
        </span>
        <span className="material-symbols-outlined ovpn-file-select-trigger-chevron" aria-hidden>
          expand_more
        </span>
      </button>
      {menu}
    </div>
  );
}
