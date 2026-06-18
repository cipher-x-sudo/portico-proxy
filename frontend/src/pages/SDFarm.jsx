import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useToast } from '../components/ui/feedback-hooks.js';
import './SDFarm.css';

const filters = [
  { id: 'all', label: 'All' },
  { id: 'valid', label: 'Ready' },
  { id: 'missing', label: 'Missing browser' },
  { id: 'ovpn', label: 'OVPN issues' },
  { id: 'duplicate', label: 'Duplicates' },
];

function statusClass(row) {
  if (row.valid) return 'success';
  if (row.browserStatus === 'duplicate' || row.ovpnStatus === 'duplicate_ovpn') return 'warning';
  return 'danger';
}

function rowIssue(row) {
  const warnings = Array.isArray(row.warnings) ? row.warnings : [];
  return warnings.length ? warnings.join('; ') : 'Ready';
}

export default function SDFarm() {
  const toast = useToast();
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [filter, setFilter] = useState('all');
  const [query, setQuery] = useState('');
  const [selectedUids, setSelectedUids] = useState([]);
  const [syncResults, setSyncResults] = useState({});

  const rows = useMemo(() => payload?.rows || [], [payload]);
  const validRows = useMemo(() => rows.filter((row) => row.valid), [rows]);

  const loadAccounts = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const res = await fetch('/api/sd-farm/accounts');
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to load SD Farm accounts');
      setPayload(data);
      setSelectedUids((current) => current.filter((uid) => (data.rows || []).some((row) => row.uid === uid)));
    } catch (err) {
      setError(err.message || 'Failed to load SD Farm accounts');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAccounts();
  }, [loadAccounts]);

  const filteredRows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return rows.filter((row) => {
      if (filter === 'valid' && !row.valid) return false;
      if (filter === 'missing' && row.browserStatus !== 'missing') return false;
      if (filter === 'ovpn' && row.ovpnStatus === 'matched') return false;
      if (filter === 'duplicate' && row.browserStatus !== 'duplicate' && row.ovpnStatus !== 'duplicate_ovpn') return false;
      if (!needle) return true;
      return [row.uid, row.name, row.openvpn, row.browserProfileName, row.routeUsername]
        .some((value) => String(value || '').toLowerCase().includes(needle));
    });
  }, [filter, query, rows]);

  const visibleValidUids = filteredRows.filter((row) => row.valid).map((row) => row.uid);
  const selectedValidCount = selectedUids.filter((uid) => validRows.some((row) => row.uid === uid)).length;
  const allVisibleValidSelected =
    visibleValidUids.length > 0 && visibleValidUids.every((uid) => selectedUids.includes(uid));

  const toggleUid = (uid) => {
    setSelectedUids((current) =>
      current.includes(uid) ? current.filter((item) => item !== uid) : [...current, uid]
    );
  };

  const toggleVisibleValid = () => {
    setSelectedUids((current) => {
      if (allVisibleValidSelected) {
        return current.filter((uid) => !visibleValidUids.includes(uid));
      }
      return Array.from(new Set([...current, ...visibleValidUids]));
    });
  };

  const previewSync = async () => {
    setBusy('preview');
    setError('');
    try {
      const res = await fetch('/api/sd-farm/preview-sync');
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Preview failed');
      setPayload(data);
      toast({
        title: 'Preview ready',
        message: `${data.validCount || 0} accounts are ready.`,
        variant: data.validCount ? 'success' : 'warning',
      });
    } catch (err) {
      setError(err.message || 'Preview failed');
      toast({ title: 'Preview failed', message: err.message || 'Preview failed', variant: 'danger' });
    } finally {
      setBusy('');
    }
  };

  const syncAccounts = async (uids) => {
    setBusy(uids.length === 1 ? uids[0] : 'sync');
    setError('');
    try {
      const res = await fetch('/api/sd-farm/sync', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ uids }),
      });
      const data = await res.json();
      if (!res.ok && res.status !== 207) throw new Error(data.error || 'Sync failed');
      const nextResults = {};
      (data.results || []).forEach((item) => {
        nextResults[item.uid] = item;
      });
      setSyncResults((current) => ({ ...current, ...nextResults }));
      toast({
        title: data.failed ? 'Sync completed with errors' : 'Sync completed',
        message: `${data.synced || 0} synced${data.failed ? `, ${data.failed} failed` : ''}.`,
        variant: data.failed ? 'warning' : 'success',
      });
      await loadAccounts();
    } catch (err) {
      setError(err.message || 'Sync failed');
      toast({ title: 'Sync failed', message: err.message || 'Sync failed', variant: 'danger' });
    } finally {
      setBusy('');
    }
  };

  if (loading && !payload) {
    return (
      <div className="loading-state">
        <span className="material-symbols-outlined loading-spinner">progress_activity</span>
        <p>Loading SD Farm accounts...</p>
      </div>
    );
  }

  return (
    <div className="sd-farm-page">
      {error && <div className="dashboard-error dashboard-error-global">{error}</div>}

      <section className="sd-farm-summary">
        <div className="sd-farm-metric">
          <span className="material-symbols-outlined">database</span>
          <div>
            <strong>{payload?.accountCount || 0}</strong>
            <small>Accounts</small>
          </div>
        </div>
        <div className="sd-farm-metric">
          <span className="material-symbols-outlined">task_alt</span>
          <div>
            <strong>{payload?.validCount || 0}</strong>
            <small>Ready</small>
          </div>
        </div>
        <div className="sd-farm-metric">
          <span className="material-symbols-outlined">warning</span>
          <div>
            <strong>{payload?.warningCount || 0}</strong>
            <small>Warnings</small>
          </div>
        </div>
        <div className={`sd-farm-metric ${payload?.ixBrowserOk ? '' : 'metric-warning'}`}>
          <span className="material-symbols-outlined">travel_explore</span>
          <div>
            <strong>{payload?.ixBrowserProfileCount || 0}</strong>
            <small>ixBrowser</small>
          </div>
        </div>
      </section>

      <section className="card sd-farm-controls">
        <div className="sd-farm-controls-row">
          <div className="search-box sd-farm-search">
            <span className="material-symbols-outlined">search</span>
            <input
              type="text"
              placeholder="Search UID, name, OVPN, profile"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
          <div className="sd-farm-actions">
            <button type="button" className="btn-secondary" onClick={loadAccounts} disabled={Boolean(busy)}>
              <span className="material-symbols-outlined">refresh</span>
              Refresh
            </button>
            <button type="button" className="btn-outline" onClick={previewSync} disabled={Boolean(busy)}>
              <span className="material-symbols-outlined">rule</span>
              Preview
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => syncAccounts(selectedUids)}
              disabled={Boolean(busy) || selectedValidCount === 0}
            >
              <span className="material-symbols-outlined">sync</span>
              Sync selected
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => syncAccounts(validRows.map((row) => row.uid))}
              disabled={Boolean(busy) || validRows.length === 0}
            >
              <span className="material-symbols-outlined">done_all</span>
              Sync all
            </button>
          </div>
        </div>
        <div className="sd-farm-filter-row">
          {filters.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`sd-farm-filter ${filter === item.id ? 'active' : ''}`}
              onClick={() => setFilter(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="sd-farm-paths">
          <span>{payload?.dbPath || ''}</span>
          {!payload?.ixBrowserOk && <strong>{payload?.ixBrowserError || 'ixBrowser unavailable'}</strong>}
        </div>
      </section>

      <section className="card p-0 overflow-hidden">
        <div className="table-header">
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined text-primary">table_rows</span>
            <h3 className="font-bold">SD Farm accounts</h3>
          </div>
          <span className="badge-primary">{filteredRows.length} ROWS</span>
        </div>
        <div className="table-container sd-farm-table-wrap">
          <table className="data-table sd-farm-table">
            <thead>
              <tr>
                <th className="sd-farm-check">
                  <input
                    type="checkbox"
                    checked={allVisibleValidSelected}
                    disabled={visibleValidUids.length === 0}
                    onChange={toggleVisibleValid}
                    aria-label="Select visible ready accounts"
                  />
                </th>
                <th>UID</th>
                <th>Name</th>
                <th>SD Farm OVPN</th>
                <th>Portico OVPN</th>
                <th>ixBrowser</th>
                <th>Route</th>
                <th>Status</th>
                <th className="sd-farm-action-col">Action</th>
              </tr>
            </thead>
            <tbody>
              {filteredRows.map((row) => {
                const result = syncResults[row.uid];
                const badgeClass = result ? (result.ok ? 'success' : 'danger') : statusClass(row);
                return (
                  <tr key={row.uid || `${row.name}-${row.openvpn}`}>
                    <td className="sd-farm-check">
                      <input
                        type="checkbox"
                        checked={selectedUids.includes(row.uid)}
                        disabled={!row.valid}
                        onChange={() => toggleUid(row.uid)}
                        aria-label={`Select ${row.uid}`}
                      />
                    </td>
                    <td className="text-mono">{row.uid || '-'}</td>
                    <td>{row.name || '-'}</td>
                    <td>{row.openvpn || '-'}</td>
                    <td>{row.matchedOvpn || '-'}</td>
                    <td>
                      <div className="sd-farm-browser-cell">
                        <strong>{row.browserProfileName || '-'}</strong>
                        {row.browserProfileId && <small>{row.browserProfileId}</small>}
                      </div>
                    </td>
                    <td className="text-mono">{row.routeUsername || '-'}</td>
                    <td>
                      <span className={`sd-farm-status sd-farm-status-${badgeClass}`}>
                        {result ? (result.ok ? 'Synced' : 'Failed') : row.valid ? 'Ready' : 'Warning'}
                      </span>
                      <p className="sd-farm-issue">{result?.error || rowIssue(row)}</p>
                    </td>
                    <td className="sd-farm-action-col">
                      <button
                        type="button"
                        className="icon-btn"
                        title="Sync account"
                        onClick={() => syncAccounts([row.uid])}
                        disabled={!row.valid || Boolean(busy)}
                      >
                        <span className="material-symbols-outlined">
                          {busy === row.uid ? 'progress_activity' : 'sync'}
                        </span>
                      </button>
                    </td>
                  </tr>
                );
              })}
              {filteredRows.length === 0 && (
                <tr>
                  <td colSpan="9" className="sd-farm-empty">
                    No accounts match the current view.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
