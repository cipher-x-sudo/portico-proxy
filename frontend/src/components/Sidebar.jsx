import React from 'react';
import './Sidebar.css';

const navItems = [
  { id: 'dashboard', icon: 'dashboard', label: 'Dashboard' },
  { id: 'status', icon: 'analytics', label: 'Status' },
  { id: 'config', icon: 'settings', label: 'Config' },
  { id: 'actions', icon: 'bolt', label: 'Actions' }
];

export default function Sidebar({ activeTab, setActiveTab, isCollapsed }) {
  return (
    <aside className={`sidebar ${isCollapsed ? 'collapsed' : ''}`}>
      <div className="sidebar-header">
        <div className="logo-box">
          <span className="material-symbols-outlined">vpn_lock</span>
        </div>
        {!isCollapsed && (
          <div className="logo-text">
            <h1>Portico</h1>
            <p>VPN proxy gateway</p>
          </div>
        )}
      </div>
      
      <nav className="nav-menu">
        {navItems.map(item => (
          <button 
            key={item.id}
            className={`nav-item ${activeTab === item.id ? 'active' : ''}`}
            onClick={() => setActiveTab(item.id)}
            title={isCollapsed ? item.label : ''}
          >
            <span className="material-symbols-outlined">{item.icon}</span>
            {!isCollapsed && <span>{item.label}</span>}
          </button>
        ))}
      </nav>
      
      <div className="sidebar-footer">
        {!isCollapsed ? (
          <div className="help-box glass-panel">
            <div className="help-header">
              <span className="material-symbols-outlined">help</span>
              <span>HELP</span>
            </div>
            <p>Need help configuring your proxy gateway?</p>
            <a href="/README.md" target="_blank" rel="noopener noreferrer">
              Open Gateway Docs
              <span className="material-symbols-outlined">open_in_new</span>
            </a>
          </div>
        ) : (
          <div className="help-box-collapsed" title="Help">
            <a href="/README.md" target="_blank" rel="noopener noreferrer">
              <span className="material-symbols-outlined">help</span>
            </a>
          </div>
        )}
      </div>
    </aside>
  );
}
