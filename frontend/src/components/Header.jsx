import React from 'react';
import { IconButton } from './ui';
import './Header.css';

export default function Header({ title, isRunning = true, toggleSidebar }) {
  return (
    <header className="header glass-panel">
      <div className="header-left">
        <IconButton className="menu-btn" icon="menu" label="Toggle navigation" onClick={toggleSidebar} />
        <h2>{title}</h2>
      </div>
      <div className="header-right">
        <div className="status-badge">
          <div className="status-indicator"></div>
          <span>{isRunning ? 'Portico online' : 'Portico offline'}</span>
        </div>
        
        <IconButton className="refresh-btn" icon="refresh" label="Refresh" />
      </div>
    </header>
  );
}
