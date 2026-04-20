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
  const [savingLauncherIdPort, setSavingLauncherIdPort] = useState(null);
  const [launcherIdFilter, setLauncherIdFilter] = useState('');
  const [error, setError] = useState('');
  const [copiedToken, setCopiedToken] = useState(null);

  const [showCreateEntry, setShowCreateEntry] = useState(false);
  const [newEntryId, setNewEntryId] = useState('');
  const [newEntryOvpn, setNewEntryOvpn] = useState('');
  const [creatingEntry, setCreatingEntry] = useState(false);

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

  const saveLauncherId = async (port, trimmedValue, previousFromServer) => {
    const next = (trimmedValue || '').trim();
    const prev = (previousFromServer || '').trim();
    if (next === prev) return;
    setSavingLauncherIdPort(port);
    setError('');
    try {
      const res = await fetch(`/api/set-launcher-id?port=${encodeURIComponent(port)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ launcherId: next }),
      });
      const data = await res.json();
      if (!data.ok) {
        setError(data.error || 'Failed to save ID');
        return;
      }
      const refreshed = await fetch('/api/status').then((r) => r.json());
      setStatus(refreshed);
      setSelectedByPort(refreshed.assignedOvpnByPort || {});
    } catch (err) {
      setError('Failed to save ID: ' + err.message);
    } finally {
      setSavingLauncherIdPort(null);
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

  const handleCreateEntry = async (e) => {
    e.preventDefault();
    const ids = newEntryId.split(/[\n,]+/).map(id => id.trim()).filter(Boolean);
    if (ids.length === 0) {
      setError('Please provide at least one ID.');
      return;
    }
    setCreatingEntry(true);
    setError('');

    try {
      const totalPortsFromApi = typeof status.totalPorts === 'number' && status.totalPorts >= 0 ? status.totalPorts : 0;
      const totalP = Math.max((status.locations || []).length, totalPortsFromApi);
      const enabledPortsSet = new Set(status.enabledPorts || []);
      const mySelectedByPort = status.assignedOvpnByPort || {};

      let unusedIdxs = [];
      for (let idx = 0; idx < totalP; idx++) {
        const loc = (status.locations || [])[idx] || {};
        const port = internalPortForIndex(status, idx);
        const portKey = String(port);
        const hasLauncherId = typeof loc.launcherId === 'string' && loc.launcherId.trim() !== '';
        const hasOvpn = !!mySelectedByPort[portKey];
        const isEnabled = enabledPortsSet.has(port);
        if (!hasLauncherId && !hasOvpn && !isEnabled) {
          unusedIdxs.push({ idx, port });
        }
      }

      if (unusedIdxs.length < ids.length) {
        throw new Error(`Only ${unusedIdxs.length} unused ports available, but ${ids.length} IDs provided.`);
      }

      // Shuffle array to pick random ports easily
      unusedIdxs = unusedIdxs.sort(() => Math.random() - 0.5);

      for (let i = 0; i < ids.length; i++) {
        const targetPort = unusedIdxs[i].port;
        const currentId = ids[i];

        const setLauncherRes = await fetch(`/api/set-launcher-id?port=${encodeURIComponent(targetPort)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ launcherId: currentId }),
        });
        
        if (!setLauncherRes.ok) {
          const errorData = await setLauncherRes.json().catch(() => ({}));
          throw new Error(errorData.error || `Failed to set Launcher ID for ${currentId}`);
        }

        if (newEntryOvpn) {
          const assignSuccess = await assignOvpn(targetPort, newEntryOvpn);
          if (!assignSuccess) {
            throw new Error(`Assigned ID ${currentId} but failed to assign OVPN file.`);
          }
        }
      }

      setNewEntryId('');
      setNewEntryOvpn('');
      setShowCreateEntry(false);
      
      // Update ui immediately
      const refreshed = await fetch('/api/status').then((r) => r.json());
      setStatus(refreshed);
      setSelectedByPort(refreshed.assignedOvpnByPort || {});
    } catch (err) {
      setError('Failed to create entry: ' + err.message);
    } finally {
      setCreatingEntry(false);
    }
  };

  const deleteEntry = async (port) => {
    if (!window.confirm("Are you sure you want to delete this entry?")) return;
    setBusyPort(port);
    setError('');
    try {
      await fetch(`/api/deactivate?port=${encodeURIComponent(port)}`, { method: 'POST' });
      await fetch(`/api/set-launcher-id?port=${encodeURIComponent(port)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ launcherId: '' }),
      });
      await fetch(`/api/assign-ovpn?port=${encodeURIComponent(port)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ovpn: '' }),
      });
      
      const refreshed = await fetch('/api/status').then(r => r.json());
      setStatus(refreshed);
      setSelectedByPort(refreshed.assignedOvpnByPort || {});
    } catch (err) {
      setError('Failed to delete entry: ' + err.message);
    } finally {
      setBusyPort(null);
    }
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

  const configuredPortRows = allPortRows.filter(({ loc, idx }) => {
    const port = internalPortForIndex(status, idx);
    const portKey = String(port);
    const hasLauncherId = typeof loc.launcherId === 'string' && loc.launcherId.trim() !== '';
    const hasOvpn = !!selectedByPort[portKey];
    const isEnabled = enabledPorts.has(port);
    return hasLauncherId || hasOvpn || isEnabled;
  });

  const launcherIdQuery = launcherIdFilter.trim().toLowerCase();
  const filteredPortRows = launcherIdQuery
    ? configuredPortRows.filter(({ loc }) => {
        const id = typeof loc.launcherId === 'string' ? loc.launcherId : '';
        return id.toLowerCase().includes(launcherIdQuery);
      })
    : configuredPortRows;

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
          <span className="badge-primary">
            {launcherIdQuery
              ? `${filteredPortRows.length} of ${configuredPortRows.length} shown`
              : `${configuredPortRows.length} entries`}
          </span>
        </div>
        <div className="dashboard-ports-launcher-toolbar flex items-center justify-between">
          <label className="dashboard-ports-launcher-search">
            <span className="material-symbols-outlined" aria-hidden>
              search
            </span>
            <input
              type="search"
              className="dashboard-ports-launcher-search-input"
              value={launcherIdFilter}
              onChange={(e) => setLauncherIdFilter(e.target.value)}
              placeholder="Search by ID…"
              aria-label="Filter ports by launcher ID"
              autoComplete="off"
              spellCheck={false}
            />
          </label>
          <button 
            type="button" 
            className="btn-primary" 
            onClick={() => setShowCreateEntry(!showCreateEntry)}
          >
            <span className="material-symbols-outlined">{showCreateEntry ? 'close' : 'add'}</span>
            {showCreateEntry ? 'Cancel' : 'Create Entry'}
          </button>
        </div>
        
        {showCreateEntry && (
          <div className="dashboard-modal-overlay" onClick={() => setShowCreateEntry(false)}>
            <div
              className="dashboard-modal-panel"
              onClick={(e) => e.stopPropagation()}
              role="dialog"
              aria-modal="true"
              aria-labelledby="dashboard-create-entry-title"
            >
              <div className="dashboard-modal-header">
                <h3 id="dashboard-create-entry-title" className="dashboard-modal-title">
                  Create New Port Entry
                </h3>
                <button
                  type="button"
                  onClick={() => setShowCreateEntry(false)}
                  className="dashboard-modal-close"
                  aria-label="Close"
                >
                  <span className="material-symbols-outlined">close</span>
                </button>
              </div>
              <form className="dashboard-modal-body" onSubmit={handleCreateEntry}>
                <label className="dashboard-modal-field">
                  <span className="dashboard-modal-label">Launcher IDs (Bulk support)</span>
                  <textarea
                    className="dashboard-modal-input"
                    value={newEntryId}
                    onChange={(e) => setNewEntryId(e.target.value)}
                    placeholder="Enter unique ID(s) separated by commas or newlines…"
                    rows={4}
                    required
                  />
                </label>
                <label className="dashboard-modal-field">
                  <span className="dashboard-modal-label">Location Configuration (Optional)</span>
                  <OvpnFileSelect
                    files={sortedOvpnFiles}
                    value={newEntryOvpn}
                    onChange={setNewEntryOvpn}
                    disabled={creatingEntry}
                    placeholder="Select location .ovpn…"
                  />
                </label>
                <div className="dashboard-modal-actions">
                  <button
                    type="submit"
                    className="btn-primary dashboard-modal-submit"
                    disabled={creatingEntry || !newEntryId.trim()}
                  >
                    {creatingEntry ? 'Creating...' : 'Create Config'}
                  </button>
                </div>
              </form>
            </div>
          </div>
        )}
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
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
                  <td colSpan="6" className="text-center p-6 text-muted">No locations configured.</td>
                </tr>
              ) : filteredPortRows.length === 0 ? (
                <tr>
                  <td colSpan="6" className="text-center p-6 text-muted">
                    No ports match this ID search.
                  </td>
                </tr>
              ) : (
                filteredPortRows.map(({ loc, idx }) => {
                  const port = internalPortForIndex(status, idx);
                  const displayPort = publishedPortForIndex(status, idx);
                  const portKey = String(port);
                  const selected = selectedByPort[portKey] || '';
                  const launcherIdServer = typeof loc.launcherId === 'string' ? loc.launcherId : '';
                  const activationState = activationStateByPort[portKey] || (enabledPorts.has(port) ? 'active' : 'inactive');
                  const isStarting = activationState === 'starting';
                  const isActive = activationState === 'active';
                  const isFailed = activationState === 'failed';
                  const canStart = !isStarting && !!selected;
                  const selectedLocation = selected ? formatOvpnRichLabel(selected) : '';
                  return (
                    <tr key={port} className={selected ? 'dashboard-row-ovpn-selected' : undefined}>
                      <td className="dashboard-launcher-id-cell">
                        <input
                          type="text"
                          className="dashboard-launcher-id-input"
                          name={`launcher-id-${port}`}
                          defaultValue={launcherIdServer}
                          key={`${port}-${launcherIdServer}`}
                          disabled={savingLauncherIdPort === port}
                          maxLength={256}
                          placeholder="—"
                          title="Your label for this port (saved when you leave the field)"
                          onBlur={(e) =>
                            saveLauncherId(port, e.target.value, launcherIdServer)
                          }
                        />
                      </td>
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
                          disabled={busyPort === port || isActive || isStarting}
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
                            <>
                              <button
                                type="button"
                                className="btn-primary"
                                disabled={busyPort === port || !canStart}
                                onClick={() => setActivation(port, true)}
                              >
                                {busyPort === port ? 'Working...' : isStarting ? 'Starting...' : isFailed ? 'Retry Start' : 'Open Port'}
                              </button>
                              <button
                                type="button"
                                className="btn-danger"
                                title="Delete this entry completely"
                                disabled={busyPort === port}
                                onClick={() => deleteEntry(port)}
                              >
                                Delete
                              </button>
                            </>
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
                              <button
                                type="button"
                                className="btn-danger"
                                title="Delete this entry completely"
                                disabled={busyPort === port || isStarting}
                                onClick={() => deleteEntry(port)}
                                style={{backgroundColor: '#e74c3c'}}
                              >
                                Delete
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
