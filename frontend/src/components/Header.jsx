import React from 'react';
import './Header.css';

export default function Header({ title, isRunning = true, toggleSidebar }) {
  return (
    <header className="header glass-panel">
      <div className="header-left">
        <button className="icon-btn menu-btn" onClick={toggleSidebar}>
          <span className="material-symbols-outlined">menu</span>
        </button>
        <h2>{title}</h2>
      </div>
      <div className="header-right">
        <div className="status-badge">
          <div className="status-indicator"></div>
          <span>{isRunning ? 'Portico online' : 'Portico offline'}</span>
        </div>
        
        <button className="icon-btn refresh-btn" title="Refresh">
          <span className="material-symbols-outlined">refresh</span>
        </button>
      </div>
    </header>
  );
}
