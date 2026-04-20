import React, { useEffect, useState, useRef } from 'react';
import { copyToClipboard } from '../utils/copyToClipboard';
import './Actions.css';

export default function Actions() {
  const [gatewayLogs, setGatewayLogs] = useState([]);
  const [logsPaused, setLogsPaused] = useState(false);
  const [logFilter, setLogFilter] = useState('');
  
  const [activeSlots, setActiveSlots] = useState([]);
  const [selectedWorkerPort, setSelectedWorkerPort] = useState('');
  const [workerLogs, setWorkerLogs] = useState('');
  
  const [evictPort, setEvictPort] = useState('');
  
  const logsViewerRef = useRef(null);

  // Fetch active slots for the worker dropdown
  useEffect(() => {
    fetch('/api/status')
      .then(res => res.json())
      .then(data => {
        if (data && data.activeSlots) {
          setActiveSlots(data.activeSlots);
        }
      })
      .catch(err => console.error("Error fetching status for workers:", err));
  }, []);

  // Fetch gateway logs periodically
  useEffect(() => {
    if (logsPaused) return;
    
    const fetchLogs = () => {
      fetch('/api/logs?tail=200')
        .then(res => res.json())
        .then(data => {
          if (data && data.lines) {
            setGatewayLogs(data.lines);
          }
        })
        .catch(err => console.error("Error fetching logs:", err));
    };

    fetchLogs();
    const interval = setInterval(fetchLogs, 3000);
    return () => clearInterval(interval);
  }, [logsPaused]);

  // Keep newest log lines visible inside the log panel only (avoid scrollIntoView — it scrolls the whole page).
  useEffect(() => {
    if (logsPaused || !logsViewerRef.current) return;
    const el = logsViewerRef.current;
    el.scrollTop = el.scrollHeight;
  }, [gatewayLogs, logsPaused]);

  const handleStopGateway = async () => {
    if (!window.confirm("Are you sure you want to stop the gateway? All active proxy sessions will be disconnected immediately.")) return;
    try {
      const res = await fetch('/api/shutdown', { method: 'POST' });
      if (res.ok) alert("Gateway shutdown initiated. The service will stop.");
    } catch (e) {
      alert("Error stopping gateway: " + e.message);
    }
  };

  const handleEvict = async () => {
    if (!evictPort) return;
    try {
      const res = await fetch(`/api/evict?port=${evictPort}`, { method: 'POST' });
      const data = await res.json();
      if (data.ok) {
        alert(`Port ${evictPort} evicted successfully.`);
        setEvictPort('');
      } else {
        alert("Failed to evict: " + data.error);
      }
    } catch (e) {
      alert("Error evicting port: " + e.message);
    }
  };

  const loadWorkerLogs = async () => {
    if (!selectedWorkerPort) return;
    try {
      setWorkerLogs("Loading...");
      const res = await fetch(`/api/worker-logs?port=${selectedWorkerPort}`);
      const data = await res.json();
      if (data.logs) {
        setWorkerLogs(data.logs);
      } else {
        setWorkerLogs("No logs available or error: " + (data.error || 'Unknown'));
      }
    } catch (e) {
      setWorkerLogs("Error fetching worker logs: " + e.message);
    }
  };

  const copyGatewayLogs = async () => {
    try {
      await copyToClipboard(gatewayLogs.join('\n'));
      alert('Logs copied to clipboard.');
    } catch (e) {
      alert(e?.message ? `Copy failed: ${e.message}` : 'Copy failed');
    }
  };

  const filteredLogs = gatewayLogs.filter(line => 
    line.toLowerCase().includes(logFilter.toLowerCase())
  );

  return (
    <div className="actions-page">
      <div className="grid-2">
        {/* Gateway Lifecycle */}
        <section className="action-section">
          <h2 className="section-label">Gateway Lifecycle</h2>
          <div className="card split-card">
            <div>
              <h3>Stop Gateway Services</h3>
              <p>Disconnect all active tunnels and shut down the proxy engine.</p>
            </div>
            <button className="btn-danger-solid" onClick={handleStopGateway}>
              <span className="material-symbols-outlined">power_settings_new</span>
              Stop Gateway
            </button>
          </div>
        </section>

        {/* Slot Management */}
        <section className="action-section">
          <h2 className="section-label">Slot Management</h2>
          <div className="card">
            <h3>Evict Container by Port</h3>
            <p className="mb-4 text-muted text-sm mt-1">Manually terminate a specific worker slot by its assigned port.</p>
            <div className="flex gap-2">
              <input 
                type="number" 
                className="premium-input flex-1" 
                placeholder="Port (e.g. 1194)" 
                value={evictPort}
                onChange={e => setEvictPort(e.target.value)}
              />
              <button className="btn-primary" onClick={handleEvict}>Evict</button>
            </div>
          </div>
        </section>
      </div>

      {/* Gateway Logs */}
      <section className="action-section">
        <div className="logs-header">
          <h2 className="section-label m-0">Gateway Logs</h2>
          <div className="logs-controls">
            <div className="search-box small">
              <span className="material-symbols-outlined">search</span>
              <input 
                type="text" 
                placeholder="Filter logs..." 
                value={logFilter}
                onChange={e => setLogFilter(e.target.value)}
              />
            </div>
            <button 
              className={`icon-btn ${logsPaused ? 'active-warning' : ''}`}
              onClick={() => setLogsPaused(!logsPaused)}
              title={logsPaused ? "Resume Stream" : "Pause Stream"}
            >
              <span className="material-symbols-outlined">{logsPaused ? 'play_arrow' : 'pause'}</span>
            </button>
            <button className="icon-btn" onClick={copyGatewayLogs} title="Copy to Clipboard">
              <span className="material-symbols-outlined">content_copy</span>
            </button>
            <button className="icon-btn" onClick={() => setGatewayLogs([])} title="Clear Logs">
              <span className="material-symbols-outlined">delete</span>
            </button>
          </div>
        </div>
        
        <div className="logs-viewer" ref={logsViewerRef}>
          {filteredLogs.length === 0 ? (
            <div className="text-muted p-4 text-sm font-mono">No logs to display...</div>
          ) : (
            filteredLogs.map((line, i) => (
              <div key={i} className="log-line">{line}</div>
            ))
          )}
        </div>
      </section>

      {/* Worker Logs */}
      <section className="action-section">
        <h2 className="section-label">Docker Worker Logs</h2>
        <div className="card">
          <div className="flex-col md-row gap-4 mb-6">
            <div className="flex-1">
              <label className="text-xs font-bold text-muted uppercase tracking-wide block mb-2">Select Active Worker Slot</label>
              <select 
                className="premium-select"
                value={selectedWorkerPort}
                onChange={e => setSelectedWorkerPort(e.target.value)}
              >
                <option value="">No active workers</option>
                {activeSlots.map(s => (
                  <option key={s.port} value={s.port}>
                    Port {s.port} - {s.locationLabel || 'Unknown'} ({s.containerName || 'Local'})
                  </option>
                ))}
              </select>
            </div>
            <div className="flex items-end">
              <button className="btn-primary" onClick={loadWorkerLogs}>
                <span className="material-symbols-outlined">terminal</span>
                View Worker Logs
              </button>
            </div>
          </div>
          
          <div className="worker-logs-viewer">
            <pre>{workerLogs || 'Select a worker and click "View Worker Logs" to load output.'}</pre>
          </div>
        </div>
      </section>
    </div>
  );
}
