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

const defaultSettings = {
  sdFarmRoot: '',
  ixBrowserApiBase: '',
  ixBrowserProxyHost: '127.0.0.1',
  useDocker: false,
  dbPath: '',
  dbError: '',
};

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
  const [settings, setSettings] = useState(defaultSettings);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [browseOpen, setBrowseOpen] = useState(false);
  const [browseLoading, setBrowseLoading] = useState(false);
  const [browseData, setBrowseData] = useState(null);
  const [browseError, setBrowseError] = useState('');

  const rows = useMemo(() => payload?.rows || [], [payload]);
  const validRows = useMemo(() => rows.filter((row) => row.valid), [rows]);

  const loadSettings = useCallback(async () => {
    try {
      const res = await fetch('/api/sd-farm/settings');
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to load SD Farm settings');
      setSettings({
        sdFarmRoot: data.sdFarmRoot || '',
        ixBrowserApiBase: data.ixBrowserApiBase || '',
        ixBrowserProxyHost: data.ixBrowserProxyHost || '127.0.0.1',
        useDocker: Boolean(data.useDocker),
        dbPath: data.dbPath || '',
        dbError: data.dbError || '',
      });
      setSettingsDirty(false);
    } catch (err) {
      setError(err.message || 'Failed to load SD Farm settings');
    }
  }, []);

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
    loadSettings();
    loadAccounts();
  }, [loadAccounts, loadSettings]);

  const handleSettingsChange = (field, value) => {
    setSettings((current) => ({ ...current, [field]: value }));
    setSettingsDirty(true);
  };

  const loadBrowse = useCallback(async (path) => {
    setBrowseLoading(true);
    setBrowseError('');
    try {
      const query = path ? `?path=${encodeURIComponent(path)}` : '';
      const res = await fetch(`/api/sd-farm/browse${query}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to browse folders');
      setBrowseData(data);
    } catch (err) {
      setBrowseError(err.message || 'Failed to browse folders');
    } finally {
      setBrowseLoading(false);
    }
  }, []);

  const openBrowse = async () => {
    setBrowseOpen(true);
    await loadBrowse(settings.sdFarmRoot || '');
  };

  const selectBrowseFolder = (path) => {
    handleSettingsChange('sdFarmRoot', path);
    setBrowseOpen(false);
    toast({
      title: 'Folder selected',
      message: path,
      variant: 'success',
    });
  };

  const browseBreadcrumbs = useMemo(() => {
    const current = browseData?.path || '';
    if (!current) return [];
    const normalized = current.replace(/\\/g, '/');
    if (normalized === '/') return [{ label: '/', path: '/' }];
    const isWindowsDrive = /^[A-Za-z]:\//.test(normalized);
    const parts = normalized.split('/').filter(Boolean);
    const crumbs = [];
    if (isWindowsDrive && parts.length > 0) {
      let acc = `${parts[0]}/`;
      crumbs.push({ label: parts[0], path: acc });
      for (let i = 1; i < parts.length; i += 1) {
        acc = `${acc.replace(/\/$/, '')}/${parts[i]}`;
        crumbs.push({ label: parts[i], path: acc });
      }
      return crumbs;
    }
    let acc = '';
    for (const part of parts) {
      acc = `${acc}/${part}`;
      crumbs.push({ label: part, path: acc });
    }
    return crumbs;
  }, [browseData?.path]);

  const saveSettings = async () => {
    setBusy('settings');
    setError('');
    try {
      const res = await fetch('/api/sd-farm/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sdFarmRoot: settings.sdFarmRoot,
          ixBrowserApiBase: settings.ixBrowserApiBase,
          ixBrowserProxyHost: settings.ixBrowserProxyHost,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to save SD Farm settings');
      setSettings({
        sdFarmRoot: data.sdFarmRoot || '',
        ixBrowserApiBase: data.ixBrowserApiBase || '',
        ixBrowserProxyHost: data.ixBrowserProxyHost || '127.0.0.1',
        useDocker: Boolean(data.useDocker),
        dbPath: data.dbPath || '',
        dbError: data.dbError || '',
      });
      setSettingsDirty(false);
      toast({
        title: 'SD Farm settings saved',
        message: data.dbPath ? `Using ${data.dbPath}` : 'Settings updated.',
        variant: 'success',
      });
      await loadAccounts();
    } catch (err) {
      setError(err.message || 'Failed to save SD Farm settings');
      toast({ title: 'Save failed', message: err.message || 'Failed to save SD Farm settings', variant: 'danger' });
    } finally {
      setBusy('');
    }
  };

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

      <section className="card sd-farm-settings">
        <div className="sd-farm-settings-header">
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined text-primary">folder_open</span>
            <h3 className="font-bold">Data source</h3>
            {settingsDirty && <span className="dirty-badge">Unsaved</span>}
          </div>
          <button
            type="button"
            className="btn-outline"
            onClick={() => setSettingsOpen((open) => !open)}
            aria-expanded={settingsOpen}
          >
            <span className="material-symbols-outlined">{settingsOpen ? 'expand_less' : 'expand_more'}</span>
            {settingsOpen ? 'Hide' : 'Configure'}
          </button>
        </div>
        {settingsOpen && (
          <div className="sd-farm-settings-body">
            <div className="form-group">
              <label htmlFor="sd-farm-root">SD Farm root folder</label>
              <div className="sd-farm-root-row">
                <input
                  id="sd-farm-root"
                  type="text"
                  className="premium-input"
                  placeholder={settings.useDocker ? '/sd-farm' : 'H:/SD Farm'}
                  value={settings.sdFarmRoot}
                  onChange={(event) => handleSettingsChange('sdFarmRoot', event.target.value)}
                />
                <button
                  type="button"
                  className="btn-outline sd-farm-browse-btn"
                  onClick={openBrowse}
                  disabled={Boolean(busy)}
                >
                  <span className="material-symbols-outlined">folder_open</span>
                  Browse
                </button>
              </div>
              {browseOpen && (
                <div className="sd-farm-browse-panel">
                  <div className="sd-farm-browse-toolbar">
                    <div className="sd-farm-browse-crumbs">
                      {browseBreadcrumbs.map((crumb) => (
                        <button
                          key={crumb.path}
                          type="button"
                          className="sd-farm-browse-crumb"
                          onClick={() => loadBrowse(crumb.path)}
                        >
                          {crumb.label}
                        </button>
                      ))}
                    </div>
                    <div className="sd-farm-browse-toolbar-actions">
                      {browseData?.parent && (
                        <button
                          type="button"
                          className="btn-outline"
                          onClick={() => loadBrowse(browseData.parent)}
                          disabled={browseLoading}
                        >
                          <span className="material-symbols-outlined">arrow_upward</span>
                          Up
                        </button>
                      )}
                      <button
                        type="button"
                        className="btn-primary"
                        onClick={() => selectBrowseFolder(browseData?.path || settings.sdFarmRoot)}
                        disabled={browseLoading || !browseData?.path}
                      >
                        Select folder
                      </button>
                      <button
                        type="button"
                        className="btn-outline"
                        onClick={() => setBrowseOpen(false)}
                      >
                        Close
                      </button>
                    </div>
                  </div>
                  {browseError && <p className="sd-farm-browse-error">{browseError}</p>}
                  {browseLoading && (
                    <div className="sd-farm-browse-loading">
                      <span className="material-symbols-outlined loading-spinner">progress_activity</span>
                      Loading folders...
                    </div>
                  )}
                  {!browseLoading && browseData && (
                    <>
                      {browseData.hasAccountsDb && (
                        <p className="sd-farm-browse-db-hint">
                          Database found here: <strong>{browseData.accountsDbPath}</strong>
                        </p>
                      )}
                      <ul className="sd-farm-browse-list">
                        {(browseData.entries || []).map((entry) => (
                          <li key={entry.path}>
                            <button
                              type="button"
                              className="sd-farm-browse-item"
                              onClick={() => loadBrowse(entry.path)}
                            >
                              <span className="material-symbols-outlined">folder</span>
                              <span className="sd-farm-browse-item-name">{entry.name}</span>
                              {entry.hasAccountsDb && (
                                <span className="sd-farm-browse-badge">accounts.sqlite</span>
                              )}
                            </button>
                          </li>
                        ))}
                        {(browseData.entries || []).length === 0 && (
                          <li className="sd-farm-browse-empty">No subfolders here.</li>
                        )}
                      </ul>
                      {browseData.truncated && (
                        <p className="sd-farm-settings-hint">Showing the first folders only.</p>
                      )}
                    </>
                  )}
                </div>
              )}
              <p className="sd-farm-settings-hint">
                {settings.useDocker
                  ? 'Use the container path (usually /sd-farm). Mount your host folder in docker-compose with SD_FARM_HOST_PATH.'
                  : 'Folder that contains DB/data/accounts.sqlite, or any folder with accounts.sqlite inside it.'}
              </p>
            </div>
            <div className="sd-farm-settings-grid">
              <div className="form-group">
                <label htmlFor="sd-farm-ix-api">ixBrowser API base</label>
                <input
                  id="sd-farm-ix-api"
                  type="text"
                  className="premium-input"
                  placeholder="http://127.0.0.1:53200/api/v2/"
                  value={settings.ixBrowserApiBase}
                  onChange={(event) => handleSettingsChange('ixBrowserApiBase', event.target.value)}
                />
              </div>
              <div className="form-group">
                <label htmlFor="sd-farm-ix-host">ixBrowser proxy host</label>
                <input
                  id="sd-farm-ix-host"
                  type="text"
                  className="premium-input"
                  placeholder="127.0.0.1"
                  value={settings.ixBrowserProxyHost}
                  onChange={(event) => handleSettingsChange('ixBrowserProxyHost', event.target.value)}
                />
              </div>
            </div>
            <div className="sd-farm-settings-footer">
              <div className="sd-farm-paths">
                <span>{settings.dbPath || settings.sdFarmRoot || 'No database selected'}</span>
                {settings.dbError && <strong>{settings.dbError}</strong>}
              </div>
              <button
                type="button"
                className="btn-primary"
                onClick={saveSettings}
                disabled={Boolean(busy) || !settingsDirty}
              >
                <span className="material-symbols-outlined">save</span>
                Save path
              </button>
            </div>
          </div>
        )}
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
