import React, { useEffect, useState } from 'react';
import './Status.css';
import { internalToPublishedPort, publishedPortForIndex } from '../utils/portDisplay';

export default function Status() {
  const [data, setData] = useState(null);
  const [search, setSearch] = useState('');
  const [autoRefresh, setAutoRefresh] = useState(true);

  useEffect(() => {
    let interval;
    const fetchStatus = () => {
      fetch('/api/status')
        .then(res => res.json())
        .then(resData => setData(resData))
        .catch(err => console.error("Error fetching status:", err));
    };

    fetchStatus();
    
    if (autoRefresh) {
      interval = setInterval(fetchStatus, 5000);
    }
    
    return () => {
      if (interval) clearInterval(interval);
    };
  }, [autoRefresh]);

  if (!data) {
    return (
      <div className="loading-state">
        <span className="material-symbols-outlined loading-spinner">progress_activity</span>
        <p>Loading gateway status...</p>
      </div>
    );
  }

  const formatActivity = (ageSeconds) => {
    if (ageSeconds == null || ageSeconds < 0) return '—';
    const s = Math.floor(Number(ageSeconds));
    if (s < 60) return s + ' s ago';
    if (s < 3600) return Math.floor(s / 60) + ' min ago';
    return Math.floor(s / 3600) + ' h ago';
  };

  const activeSlots = data.activeSlots || [];
  const filteredSlots = activeSlots.filter(s => {
    const pub = internalToPublishedPort(data, s.port);
    const text = [s.port, pub, s.locationLabel, s.proxyType, s.containerName].join(' ').toLowerCase();
    return text.includes(search.toLowerCase());
  });

  const locations = data.locations || [];
  const filteredLocations = locations.filter((loc, i) => {
    const pub = publishedPortForIndex(data, i);
    const text = [i, loc.label, loc.ovpn, data.portBase + i, pub].join(' ').toLowerCase();
    return text.includes(search.toLowerCase());
  });

  const publishedMax =
    data.publishedPortBase != null && typeof data.publishedPortBase === 'number' && data.portMax != null
      ? data.publishedPortBase + (data.portMax - data.portBase)
      : null;
  const publishedRange =
    data.publishedPortBase != null && publishedMax != null
      ? `${data.publishedPortBase} - ${publishedMax}`
      : null;

  return (
    <div className="status-page">
      <div className="status-header-actions">
        <div>
          <h2>System Status</h2>
          <p className="text-muted text-sm mt-1">Real-time health monitoring and active proxy connections.</p>
        </div>
        
        <div className="flex gap-2">
          <label className="toggle-label glass-panel">
            <span className="text-xs font-bold text-muted">AUTO-REFRESH</span>
            <input 
              type="checkbox" 
              className="toggle-checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)} 
            />
            <div className="toggle-slider"></div>
          </label>
        </div>
      </div>

      <section className="summary-grid">
        <div className="card stat-card">
          <p className="stat-label">Status</p>
          <div className="stat-value flex items-center gap-2">
            <span className={`status-indicator ${data.running ? '' : 'stopped'}`}></span>
            {data.running ? 'Running' : 'Stopped'}
          </div>
        </div>
        <div className="card stat-card">
          <p className="stat-label">Port Range</p>
          <p className="stat-value">
            {publishedRange ? (
              <>
                {publishedRange}
                <span className="text-muted text-sm block font-normal mt-1">Host (connect here)</span>
              </>
            ) : (
              `${data.portBase} - ${data.portMax || 'N/A'}`
            )}
          </p>
          {publishedRange && (
            <p className="stat-value text-muted text-sm mt-1 mb-0">
              Container {data.portBase} - {data.portMax || 'N/A'}
            </p>
          )}
        </div>
        <div className="card stat-card">
          <p className="stat-label">Active Slots</p>
          <p className="stat-value">{activeSlots.length} / {data.maxSlots || 'N/A'}</p>
        </div>
        <div className="card stat-card">
          <p className="stat-label">Idle Timeout</p>
          <p className="stat-value">{data.idleTimeoutMinutes} min</p>
        </div>
        <div className="card stat-card">
          <p className="stat-label">Backend</p>
          <p className="stat-value">{data.useDocker ? 'Docker' : 'Local'}</p>
        </div>
        <div className="card stat-card col-span-2">
          <p className="stat-label">Listen Host</p>
          <p className="stat-value">{data.listenHost || '127.0.0.1'} <span className="text-muted text-sm">: {data.controlPort || 'N/A'} (Control)</span></p>
        </div>
      </section>

      <div className="filter-bar">
        <div className="search-box">
          <span className="material-symbols-outlined">search</span>
          <input 
            type="text" 
            placeholder="Filter by port, location..." 
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>

      <section className="card p-0 overflow-hidden">
        <div className="table-header">
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined text-primary">lan</span>
            <h3 className="font-bold">Active Slots</h3>
          </div>
          <span className="badge-primary">{activeSlots.length} SESSIONS</span>
        </div>
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{publishedRange ? 'Host port' : 'Port'}</th>
                <th>Location</th>
                <th>Type</th>
                <th>Last Activity</th>
                <th>Backend</th>
                <th className="text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filteredSlots.length === 0 ? (
                <tr>
                  <td colSpan="6" className="text-center p-6 text-muted">No active slots found. Gateways are idle.</td>
                </tr>
              ) : (
                filteredSlots.map(s => {
                  const hostPort = internalToPublishedPort(data, s.port);
                  return (
                  <tr key={s.port}>
                    <td className="text-primary text-mono font-bold">
                      {hostPort}
                      {hostPort !== s.port && (
                        <div className="text-muted text-xs font-normal">Container: {s.port}</div>
                      )}
                    </td>
                    <td>
                      <div className="flex gap-2 items-center">
                        <span className="material-symbols-outlined text-sm text-muted">public</span>
                        {s.locationLabel || `#${s.locationIndex}`}
                      </div>
                    </td>
                    <td><span className="badge-outline">{s.proxyType || 'HTTP'}</span></td>
                    <td className="text-muted">{formatActivity(s.lastActivityAgeSeconds)}</td>
                    <td className="text-xs text-mono">{s.containerName || `proxy-${s.port}`}</td>
                    <td className="text-right">
                      <button className="btn-danger">Evict</button>
                    </td>
                  </tr>
                );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card p-0 overflow-hidden">
        <div className="table-header">
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined text-primary">map</span>
            <h3 className="font-bold">Port Map / Locations List</h3>
          </div>
        </div>
        <div className="table-container max-h-96">
          <table className="data-table">
            <thead>
              <tr>
                <th>Index</th>
                <th>Label</th>
                <th>OVPN Filename</th>
                <th>{publishedRange ? 'Host proxy port' : 'Proxy port'}</th>
              </tr>
            </thead>
            <tbody>
              {filteredLocations.map((loc, i) => {
                const hostP = publishedPortForIndex(data, i);
                const containerP = data.portBase + i;
                return (
                <tr key={i}>
                  <td className="text-muted text-mono">{i}</td>
                  <td className="font-medium">{loc.label}</td>
                  <td className="text-xs text-muted text-mono">{loc.ovpn}</td>
                  <td className="text-primary text-mono font-bold">
                    {hostP}
                    {hostP !== containerP && (
                      <div className="text-muted text-xs font-normal">Container: {containerP}</div>
                    )}
                  </td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
