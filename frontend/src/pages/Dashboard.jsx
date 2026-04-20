import React, { useEffect, useMemo, useState } from 'react';
import './Dashboard.css';
import OvpnFileSelect from '../components/OvpnFileSelect';
import { formatOvpnDisplayLabel, formatOvpnRichLabel, sortOvpnFiles } from '../utils/ovpnFiles';
import { copyToClipboard } from '../utils/copyToClipboard';
import { internalPortForIndex, internalToPublishedPort, publishedPortForIndex } from '../utils/portDisplay';

export default function Dashboard() {
  const [status, setStatus] = useState(null);
  const [ovpnFiles, setOvpnFiles] = useState([]);
  const [ovpnFilesHint, setOvpnFilesHint] = useState('');
  const [selectedByPort, setSelectedByPort] = useState({});
  const [busyPort, setBusyPort] = useState(null);
  const [error, setError] = useState('');
  const [copiedToken, setCopiedToken] = useState(null);

  useEffect(() => {
    const loadStatus = () => {
      fetch('/api/status')
        .then(res => res.json())
        .then(data => {
          setStatus(data);
          // Server is source of truth (include every listener port, often ""). Do not merge prev on top — that
          // overwrote saved assignments with "" when the placeholder or stale state won.
          setSelectedByPort(data.assignedOvpnByPort || {});
        })
        .catch(err => console.error("Error fetching status:", err));
    };
    const loadFiles = () => {
      fetch('/api/ovpn-files')
        .then(res => res.json())
        .then(data => {
          setOvpnFiles(Array.isArray(data.files) ? data.files : []);
          setOvpnFilesHint(typeof data.hint === 'string' ? data.hint : '');
        })
        .catch(err => console.error("Error fetching ovpn files:", err));
    };
    loadStatus();
    loadFiles();
    const id = setInterval(loadStatus, 5000);
    return () => clearInterval(id);
  }, []);

  const sortedOvpnFiles = useMemo(() => sortOvpnFiles(ovpnFiles), [ovpnFiles]);

  const assignOvpn = async (port, ovpn) => {
    setBusyPort(port);
    setError('');
    try {
      const res = await fetch(`/api/assign-ovpn?port=${encodeURIComponent(port)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ovpn: ovpn || '' }),
      });
      const data = await res.json();
      if (!data.ok) {
        setError(data.error || 'Failed to assign ovpn file');
        return false;
      }
      const refreshed = await fetch('/api/status').then((r) => r.json());
      setStatus(refreshed);
      setSelectedByPort(refreshed.assignedOvpnByPort || {});
      return true;
    } catch (err) {
      setError('Failed to assign ovpn file: ' + err.message);
      return false;
    } finally {
      setBusyPort(null);
    }
  };

  const setActivation = async (port, activate) => {
    setBusyPort(port);
    setError('');
    try {
      const endpoint = activate ? '/api/activate' : '/api/deactivate';
      const res = await fetch(`${endpoint}?port=${encodeURIComponent(port)}`, { method: 'POST' });
      const data = await res.json();
      if (!data.ok) {
        setError(data.error || `Failed to ${activate ? 'activate' : 'deactivate'} port`);
        return;
      }
      const refreshed = await fetch('/api/status').then(r => r.json());
      setStatus(refreshed);
      setSelectedByPort(refreshed.assignedOvpnByPort || {});
    } catch (err) {
      setError(`Failed to ${activate ? 'activate' : 'deactivate'} port: ` + err.message);
    } finally {
      setBusyPort(null);
    }
  };

  const extendPort = async (port) => {
    setBusyPort(port);
    setError('');
    try {
      const res = await fetch(`/api/extend-port?port=${encodeURIComponent(port)}`, { method: 'POST' });
      const data = await res.json();
      if (!data.ok) {
        setError(data.error || 'Extend failed');
        return;
      }
      const refreshed = await fetch('/api/status').then((r) => r.json());
      setStatus(refreshed);
      setSelectedByPort(refreshed.assignedOvpnByPort || {});
    } catch (err) {
      setError('Extend failed: ' + err.message);
    } finally {
      setBusyPort(null);
    }
  };

  const onSelectRowFile = async (port, ovpn) => {
    await assignOvpn(port, ovpn);
  };

  if (!status) {
    return (
      <div className="loading-state">
        <span className="material-symbols-outlined loading-spinner">progress_activity</span>
        <p>Loading gateway status...</p>
      </div>
    );
  }

  const enabledPorts = new Set(status.enabledPorts || []);
  const locations = status.locations || [];
  const activationStateByPort = status.activationStateByPort || {};
  const activationErrorByPort = status.activationErrorByPort || {};

  const proxyHost = status.clientProxyHost || '127.0.0.1';
  const proxyUser = status.proxyUsername ?? '';
  const proxyPass = status.proxyPassword ?? '';

  const runningProxyRows = [];
  locations.forEach((loc, idx) => {
    const internalPort = internalPortForIndex(status, idx);
    if (internalPort == null) return;
    const portKey = String(internalPort);
    if (activationStateByPort[portKey] !== 'active') return;
    const hostPort = internalToPublishedPort(status, internalPort);
    const colonFormat = `${proxyHost}:${hostPort}:${proxyUser}:${proxyPass}`;
    const atFormat = `${proxyHost}:${hostPort}@${proxyUser}:${proxyPass}`;
    const selected = selectedByPort[portKey] || '';
    const fileLabel = selected ? formatOvpnDisplayLabel(selected) : '';
    runningProxyRows.push({
      internalPort,
      hostPort,
      colonFormat,
      atFormat,
      label: fileLabel || loc.label || `Location #${idx}`,
    });
  });

  /** One row per gateway listener port. */
  const totalPortsFromApi =
    typeof status.totalPorts === 'number' && status.totalPorts >= 0 ? status.totalPorts : 0;
  const totalPorts = Math.max(locations.length, totalPortsFromApi);
  const allPortRows = [];
  for (let idx = 0; idx < totalPorts; idx++) {
    const loc = locations[idx] || { label: `Port ${idx}`, randomAccess: false };
    allPortRows.push({ loc, idx });
  }

  const portColumnLabel =
    status.publishedPortBase != null && typeof status.publishedPortBase === 'number'
      ? 'Host proxy port'
      : 'Port';

  const copyProxyLine = async (text, token) => {
    try {
      await copyToClipboard(text);
      setCopiedToken(token);
      window.setTimeout(() => {
        setCopiedToken((t) => (t === token ? null : t));
      }, 2000);
    } catch (err) {
      setError(err?.message ? `Copy failed: ${err.message}` : 'Copy failed');
    }
  };

  return (
    <div className="dashboard">
      {error && <div className="dashboard-error dashboard-error-global">{error}</div>}
      {ovpnFiles.length === 0 && ovpnFilesHint && (
        <div className="dashboard-ovpn-hint dashboard-ovpn-hint-global" role="status">
          <span className="material-symbols-outlined">folder_off</span>
          <div>
            <strong>No .ovpn files listed</strong>
            <p className="text-muted text-sm mt-1 mb-0">{ovpnFilesHint}</p>
          </div>
        </div>
      )}

      <section className="card p-0 overflow-hidden dashboard-running-proxies">
        <div className="table-header">
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined text-primary">content_copy</span>
            <h3 className="font-bold">Running proxies</h3>
          </div>
          <span className="badge-primary">{runningProxyRows.length} ACTIVE</span>
        </div>
        <p className="text-muted text-sm px-4 pt-2 pb-0 mb-0">
          Click a line to copy. Host is <code className="text-mono">{proxyHost}</code>
          {status.publishedPortBase != null && typeof status.publishedPortBase === 'number' && (
            <span> (host port offset from container)</span>
          )}
          .
        </p>
        <div className="table-container">
          {runningProxyRows.length === 0 ? (
            <div className="text-center p-6 text-muted">
              No active proxies. Open a port in the launcher below to see copy-ready strings.
            </div>
          ) : (
            <table className="data-table dashboard-copy-table">
              <thead>
                <tr>
                  <th>Location</th>
                  <th>Host port</th>
                  <th>
                    <span className="block">host:port:user:pass</span>
                    <span className="text-muted text-xs font-normal">colon format</span>
                  </th>
                  <th>
                    <span className="block">host:port@user:pass</span>
                    <span className="text-muted text-xs font-normal">at format</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {runningProxyRows.map((row) => {
                  const tokColon = `colon-${row.internalPort}`;
                  const tokAt = `at-${row.internalPort}`;
                  return (
                    <tr key={row.internalPort}>
                      <td className="font-medium">{row.label}</td>
                      <td className="text-primary text-mono font-bold">{row.hostPort}</td>
                      <td>
                        <button
                          type="button"
                          className="dashboard-copy-line"
                          title="Click to copy"
                          onClick={() => copyProxyLine(row.colonFormat, tokColon)}
                        >
                          <code className="dashboard-copy-code">{row.colonFormat}</code>
                          {copiedToken === tokColon && (
                            <span className="dashboard-copy-toast">Copied</span>
                          )}
                        </button>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="dashboard-copy-line"
                          title="Click to copy"
                          onClick={() => copyProxyLine(row.atFormat, tokAt)}
                        >
                          <code className="dashboard-copy-code">{row.atFormat}</code>
                          {copiedToken === tokAt && (
                            <span className="dashboard-copy-toast">Copied</span>
                          )}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section className="card p-0 overflow-hidden">
        <div className="table-header">
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined text-primary">table_view</span>
            <h3 className="font-bold">Ports Launcher</h3>
          </div>
          <span className="badge-primary">{totalPorts} locations</span>
        </div>
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{portColumnLabel}</th>
                <th>Location</th>
                <th>Selected OVPN File</th>
                <th>Status</th>
                <th className="text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {totalPorts === 0 || locations.length === 0 ? (
                <tr>
                  <td colSpan="5" className="text-center p-6 text-muted">No locations configured.</td>
                </tr>
              ) : (
                allPortRows.map(({ loc, idx }) => {
                  const port = internalPortForIndex(status, idx);
                  const displayPort = publishedPortForIndex(status, idx);
                  const portKey = String(port);
                  const selected = selectedByPort[portKey] || '';
                  const activationState = activationStateByPort[portKey] || (enabledPorts.has(port) ? 'active' : 'inactive');
                  const isStarting = activationState === 'starting';
                  const isActive = activationState === 'active';
                  const isFailed = activationState === 'failed';
                  const canStart = !isStarting && !!selected;
                  const selectedLocation = selected ? formatOvpnRichLabel(selected) : '';
                  return (
                    <tr key={port} className={selected ? 'dashboard-row-ovpn-selected' : undefined}>
                      <td className="text-primary text-mono font-bold">
                        {displayPort}
                        {displayPort !== port && (
                          <div className="text-muted text-xs font-normal">Container: {port}</div>
                        )}
                      </td>
                      <td>
                        {selectedLocation || loc.label || `Location #${idx}`}
                        {selectedLocation && (
                          <div className="text-muted text-xs">
                            Config: {loc.label || `Location #${idx}`}
                          </div>
                        )}
                      </td>
                      <td>
                        <OvpnFileSelect
                          files={sortedOvpnFiles}
                          value={selected}
                          onChange={(file) => onSelectRowFile(port, file)}
                          disabled={busyPort === port}
                          placeholder="Select profile…"
                        />
                      </td>
                      <td>
                        <span className={isActive ? 'status-active' : isStarting ? 'status-starting' : isFailed ? 'status-failed' : 'status-inactive'}>
                          {isActive ? 'Active' : isStarting ? 'Starting...' : isFailed ? 'Failed' : 'Inactive'}
                        </span>
                        {isFailed && activationErrorByPort[portKey] && (
                          <div className="status-error-text">{activationErrorByPort[portKey]}</div>
                        )}
                      </td>
                      <td className="text-right">
                        <div className="dashboard-row-actions">
                          {!isActive ? (
                            <button
                              type="button"
                              className="btn-primary"
                              disabled={busyPort === port || !canStart}
                              onClick={() => setActivation(port, true)}
                            >
                              {busyPort === port ? 'Working...' : isStarting ? 'Starting...' : isFailed ? 'Retry Start' : 'Open Port'}
                            </button>
                          ) : (
                            <>
                              <button
                                type="button"
                                className="btn-secondary"
                                disabled={busyPort === port || isStarting}
                                onClick={() => extendPort(port)}
                                title="Add 30 minutes before idle auto-close (no proxy traffic)."
                              >
                                {busyPort === port ? 'Working...' : 'Extend +30m'}
                              </button>
                              <button
                                type="button"
                                className="btn-danger"
                                disabled={busyPort === port || isStarting}
                                onClick={() => setActivation(port, false)}
                              >
                                {busyPort === port ? 'Working...' : 'Stop Port'}
                              </button>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
