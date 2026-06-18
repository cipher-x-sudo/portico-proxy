import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useToast } from '../components/ui/feedback-hooks.js';
import {
  findAccountsDbInDirectoryHandle,
  findAccountsDbInWebkitFiles,
  importAccountsFile,
  labelFromWebkitFile,
  supportsDirectoryPicker,
} from './sdFarmImport.js';
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
  sdFarmSource: 'import',
  sdFarmImportedAt: '',
  hasImportedDb: false,
  ixBrowserApiBase: '',
  ixBrowserProxyHost: '127.0.0.1',
  ixBrowserProxyType: 'http',
  ixBrowserOk: false,
  ixBrowserError: '',
  ixBrowserProfileCount: 0,
  ixBrowserTriedUrls: [],
  ixBrowserRecommendedBase: '',
  ixBrowserHint: '',
  wslHostIp: '',
  routeMapCount: 0,
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

function parseSearchQuery(raw) {
  const lines = String(raw || '')
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
  if (lines.length > 1) return { mode: 'bulkUid', uids: new Set(lines) };
  return { mode: 'text', needle: (lines[0] || '').toLowerCase() };
}

export default function SDFarm() {
  const toast = useToast();
  const folderInputRef = useRef(null);
  const routeImportInputRef = useRef(null);
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
  const [importing, setImporting] = useState(false);
  const [ixTesting, setIxTesting] = useState(false);

  const rows = useMemo(() => payload?.rows || [], [payload]);
  const validRows = useMemo(() => rows.filter((row) => row.valid), [rows]);

  const applySettingsPayload = useCallback((data) => {
    setSettings({
      sdFarmRoot: data.sdFarmRoot || '',
      sdFarmSource: data.sdFarmSource || 'import',
      sdFarmImportedAt: data.sdFarmImportedAt || '',
      hasImportedDb: Boolean(data.hasImportedDb),
      ixBrowserApiBase: data.ixBrowserApiBase || '',
      ixBrowserProxyHost: data.ixBrowserProxyHost || '127.0.0.1',
      ixBrowserProxyType: data.ixBrowserProxyType === 'socks5' ? 'socks5' : 'http',
      ixBrowserOk: Boolean(data.ixBrowserOk),
      ixBrowserError: data.ixBrowserError || '',
      ixBrowserProfileCount: Number(data.ixBrowserProfileCount || 0),
      ixBrowserTriedUrls: Array.isArray(data.ixBrowserTriedUrls) ? data.ixBrowserTriedUrls : [],
      ixBrowserRecommendedBase: data.ixBrowserRecommendedBase || '',
      ixBrowserHint: data.ixBrowserHint || '',
      wslHostIp: data.wslHostIp || '',
      routeMapCount: typeof data.routeMapCount === 'number' ? data.routeMapCount : 0,
      useDocker: Boolean(data.useDocker),
      dbPath: data.dbPath || '',
      dbError: data.dbError || '',
    });
    setSettingsDirty(false);
  }, []);

  const loadSettings = useCallback(async () => {
    try {
      const res = await fetch('/api/sd-farm/settings');
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to load SD Farm settings');
      applySettingsPayload(data);
    } catch (err) {
      setError(err.message || 'Failed to load SD Farm settings');
    }
  }, [applySettingsPayload]);

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

  const runImport = useCallback(async (file, label, fallbackLabel = '') => {
    const folderLabel = (label || settings.sdFarmRoot || fallbackLabel || '').trim();
    if (!folderLabel) {
      throw new Error('Enter a folder label such as D:\\WORK\\SD Farm');
    }
    setImporting(true);
    setError('');
    try {
      const data = await importAccountsFile(file, folderLabel);
      applySettingsPayload(data);
      toast({
        title: 'SD Farm imported',
        message: `${data.accountCount || 0} accounts from ${folderLabel}`,
        variant: 'success',
      });
      await loadAccounts();
      return data;
    } finally {
      setImporting(false);
    }
  }, [applySettingsPayload, loadAccounts, settings.sdFarmRoot, toast]);

  const pickFolderWithDirectoryPicker = useCallback(async () => {
    const dirHandle = await window.showDirectoryPicker();
    const file = await findAccountsDbInDirectoryHandle(dirHandle);
    if (!file) {
      throw new Error('Could not find accounts.sqlite in the selected folder');
    }
    const label = settings.sdFarmRoot.trim() || dirHandle.name;
    return runImport(file, label, dirHandle.name);
  }, [runImport, settings.sdFarmRoot]);

  const pickFolder = useCallback(async () => {
    if (supportsDirectoryPicker()) {
      try {
        await pickFolderWithDirectoryPicker();
      } catch (err) {
        if (err?.name === 'AbortError') return;
        throw err;
      }
      return;
    }
    folderInputRef.current?.click();
  }, [pickFolderWithDirectoryPicker]);

  const handleFolderInputChange = async (event) => {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    if (!files.length) return;
    const file = findAccountsDbInWebkitFiles(files);
    if (!file) {
      setError('Could not find accounts.sqlite in the selected folder');
      toast({ title: 'Import failed', message: 'Could not find accounts.sqlite in the selected folder', variant: 'danger' });
      return;
    }
    const derivedLabel = labelFromWebkitFile(file);
    const label = settings.sdFarmRoot.trim() || derivedLabel;
    try {
      await runImport(file, label, derivedLabel);
    } catch (err) {
      setError(err.message || 'Import failed');
      toast({ title: 'Import failed', message: err.message || 'Import failed', variant: 'danger' });
    }
  };

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
          ixBrowserProxyType: settings.ixBrowserProxyType,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to save SD Farm settings');
      applySettingsPayload(data);
      toast({
        title: 'Settings saved',
        message: data.ixBrowserOk
          ? `ixBrowser connected (${data.ixBrowserProfileCount || 0} profiles).`
          : data.ixBrowserError || 'Settings updated.',
        variant: data.ixBrowserOk ? 'success' : 'warning',
      });
      await loadAccounts();
    } catch (err) {
      setError(err.message || 'Failed to save SD Farm settings');
      toast({ title: 'Save failed', message: err.message || 'Failed to save SD Farm settings', variant: 'danger' });
    } finally {
      setBusy('');
    }
  };

  const testIxBrowser = async () => {
    setIxTesting(true);
    setError('');
    try {
      const query = settings.ixBrowserApiBase
        ? `?ixBrowserApiBase=${encodeURIComponent(settings.ixBrowserApiBase)}`
        : '';
      const res = await fetch(`/api/sd-farm/ixbrowser-test${query}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'ixBrowser test failed');
      const workingBase = data.recommendedBase || data.ixBrowserApiBase || settings.ixBrowserApiBase;
      setSettings((current) => ({
        ...current,
        ixBrowserOk: Boolean(data.ok),
        ixBrowserError: data.ixBrowserError || data.error || '',
        ixBrowserProfileCount: Number(data.ixBrowserProfileCount || 0),
        ixBrowserTriedUrls: Array.isArray(data.triedUrls) ? data.triedUrls : [],
        ixBrowserRecommendedBase: data.recommendedBase || '',
        ixBrowserHint: data.hint || '',
        wslHostIp: data.wslHostIp || current.wslHostIp || '',
        ...(data.ok && workingBase ? { ixBrowserApiBase: workingBase } : {}),
      }));
      if (data.ok && workingBase && workingBase !== settings.ixBrowserApiBase) {
        setSettingsDirty(true);
      }
      toast({
        title: data.ok ? 'ixBrowser connected' : 'ixBrowser connection failed',
        message: data.ok
          ? `${data.ixBrowserProfileCount || 0} profiles at ${workingBase}`
          : data.ixBrowserError || data.error || 'Connection refused',
        variant: data.ok ? 'success' : 'danger',
      });
    } catch (err) {
      setError(err.message || 'ixBrowser test failed');
      toast({ title: 'ixBrowser test failed', message: err.message || 'ixBrowser test failed', variant: 'danger' });
    } finally {
      setIxTesting(false);
    }
  };

  const useDetectedIxUrl = () => {
    const next = settings.ixBrowserRecommendedBase;
    if (!next) return;
    handleSettingsChange('ixBrowserApiBase', next);
    toast({
      title: 'URL applied',
      message: next,
      variant: 'success',
    });
  };

  const handlePickFolder = async () => {
    setBusy('import');
    setError('');
    try {
      await pickFolder();
    } catch (err) {
      if (err?.name === 'AbortError') return;
      setError(err.message || 'Folder import failed');
      toast({ title: 'Import failed', message: err.message || 'Folder import failed', variant: 'danger' });
    } finally {
      setBusy('');
    }
  };

  const searchParsed = useMemo(() => parseSearchQuery(query), [query]);

  const filteredRows = useMemo(() => {
    return rows.filter((row) => {
      if (filter === 'valid' && !row.valid) return false;
      if (filter === 'missing' && row.browserStatus !== 'missing') return false;
      if (filter === 'ovpn' && row.ovpnStatus === 'matched') return false;
      if (filter === 'duplicate' && row.browserStatus !== 'duplicate' && row.ovpnStatus !== 'duplicate_ovpn') return false;
      if (searchParsed.mode === 'bulkUid') {
        return searchParsed.uids.has(String(row.uid || '').trim());
      }
      if (!searchParsed.needle) return true;
      return [row.uid, row.name, row.openvpn, row.browserProfileName, row.routeUsername]
        .some((value) => String(value || '').toLowerCase().includes(searchParsed.needle));
    });
  }, [filter, rows, searchParsed]);

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
        body: JSON.stringify({
          uids,
          proxyType: settings.ixBrowserProxyType,
        }),
      });
      const raw = await res.text();
      let data;
      try {
        data = raw ? JSON.parse(raw) : {};
      } catch {
        throw new Error(raw.slice(0, 160) || 'Sync failed: server returned non-JSON response');
      }
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

  const downloadTextFile = (filename, text, mimeType = 'application/json') => {
    const blob = new Blob([text], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  };

  const exportRoutes = async (format = 'json', uids = selectedUids) => {
    if (!uids.length) {
      toast({
        title: 'Nothing selected',
        message: 'Select accounts in the table to export their routes.',
        variant: 'warning',
      });
      return;
    }
    setBusy('export-routes');
    setError('');
    try {
      const exportRows = rows.filter((row) => uids.includes(row.uid));
      const routes = exportRows.map((row) => ({
        uid: row.uid,
        routeUsername: row.routeUsername || `sd_${row.uid}`,
        name: row.name || '',
      }));
      if (format === 'csv') {
        const lines = ['uid,routeUsername,name'];
        routes.forEach(({ uid, routeUsername, name }) => {
          const escaped = String(name).replace(/"/g, '""');
          lines.push(`${uid},${routeUsername},"${escaped}"`);
        });
        downloadTextFile('sd-farm-routes.csv', `${lines.join('\n')}\n`, 'text/csv');
      } else {
        downloadTextFile(
          'sd-farm-routes.json',
          `${JSON.stringify(
            {
              version: 1,
              exportedAt: new Date().toISOString(),
              routeMapCount: routes.length,
              routes,
            },
            null,
            2,
          )}\n`,
          'application/json',
        );
      }
      toast({
        title: 'Routes exported',
        message: `${routes.length} selected route${routes.length === 1 ? '' : 's'} exported. Import on your other PC.`,
        variant: 'success',
      });
    } catch (err) {
      setError(err.message || 'Export failed');
      toast({ title: 'Export failed', message: err.message || 'Export failed', variant: 'danger' });
    } finally {
      setBusy('');
    }
  };

  const importRoutesText = async (text, mode = 'merge') => {
    setBusy('import-routes');
    setError('');
    try {
      const res = await fetch('/api/sd-farm/import-routes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, mode }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || 'Import failed');
      await Promise.all([loadSettings(), loadAccounts()]);
      toast({
        title: 'Routes imported',
        message: `${data.imported || 0} route mapping${data.imported === 1 ? '' : 's'} applied (${data.routeMapCount || 0} total).`,
        variant: 'success',
      });
    } catch (err) {
      setError(err.message || 'Import failed');
      toast({ title: 'Import failed', message: err.message || 'Import failed', variant: 'danger' });
    } finally {
      setBusy('');
    }
  };

  const handleRouteImportFile = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    try {
      await importRoutesText(await file.text(), 'merge');
    } catch (err) {
      setError(err.message || 'Import failed');
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
            <input
              ref={folderInputRef}
              type="file"
              className="sd-farm-folder-input"
              webkitdirectory=""
              directory=""
              onChange={handleFolderInputChange}
            />
            <div className="form-group">
              <label htmlFor="sd-farm-root">SD Farm folder label</label>
              <div className="sd-farm-root-row">
                <input
                  id="sd-farm-root"
                  type="text"
                  className="premium-input"
                  placeholder="D:\WORK\SD Farm"
                  value={settings.sdFarmRoot}
                  onChange={(event) => handleSettingsChange('sdFarmRoot', event.target.value)}
                />
                <button
                  type="button"
                  className="btn-outline sd-farm-browse-btn"
                  onClick={handlePickFolder}
                  disabled={Boolean(busy) || importing}
                >
                  <span className="material-symbols-outlined">folder_open</span>
                  Select folder
                </button>
                <button
                  type="button"
                  className="btn-outline sd-farm-browse-btn"
                  onClick={handlePickFolder}
                  disabled={Boolean(busy) || importing || !settings.hasImportedDb}
                >
                  <span className="material-symbols-outlined">sync</span>
                  Re-import
                </button>
              </div>
              <p className="sd-farm-settings-hint">
                Pick your SD Farm folder on this PC. The path is saved as a label only; Portico imports
                accounts.sqlite into its own storage. Use Re-import when SD Farm updates the database.
              </p>
              {settings.sdFarmImportedAt && (
                <p className="sd-farm-settings-hint">
                  Last imported: {settings.sdFarmImportedAt}
                </p>
              )}
            </div>

            <div className="sd-farm-settings-section sd-farm-ix-section">
              <div className="sd-farm-settings-section-header">
                <div className="flex items-center gap-2">
                  <span className="material-symbols-outlined text-primary">travel_explore</span>
                  <h4 className="font-bold">ixBrowser</h4>
                  <span className={`sd-farm-ix-status ${settings.ixBrowserOk ? 'ok' : 'error'}`}>
                    {settings.ixBrowserOk
                      ? `${settings.ixBrowserProfileCount} profiles`
                      : 'Offline'}
                  </span>
                </div>
                <button
                  type="button"
                  className="btn-outline"
                  onClick={testIxBrowser}
                  disabled={Boolean(busy) || ixTesting}
                >
                  <span className="material-symbols-outlined">
                    {ixTesting ? 'progress_activity' : 'lan'}
                  </span>
                  Test connection
                </button>
              </div>
              <div className="sd-farm-settings-grid">
                <div className="form-group">
                  <label htmlFor="sd-farm-ix-api">API base URL</label>
                  <input
                    id="sd-farm-ix-api"
                    type="text"
                    className="premium-input"
                    placeholder={settings.useDocker
                      ? 'http://host.docker.internal:53200/api/v2/'
                      : 'http://127.0.0.1:53200/api/v2/'}
                    value={settings.ixBrowserApiBase}
                    onChange={(event) => handleSettingsChange('ixBrowserApiBase', event.target.value)}
                  />
                  <p className="sd-farm-settings-hint">
                    {settings.useDocker
                      ? (settings.wslHostIp
                        ? `Auto-detect tries host.docker.internal (Docker Desktop) and your Windows host IP (${settings.wslHostIp}, WSL Docker). Test connection picks the first URL that responds.`
                        : 'Auto-detect tries host.docker.internal (Docker Desktop). On WSL Docker, the Windows host IP is detected automatically when possible.')
                      : 'Local gateway: use http://127.0.0.1:53200/api/v2/ when ixBrowser runs on the same machine.'}
                  </p>
                </div>
                <div className="form-group">
                  <label htmlFor="sd-farm-ix-host">Proxy host for profiles</label>
                  <input
                    id="sd-farm-ix-host"
                    type="text"
                    className="premium-input"
                    placeholder="127.0.0.1"
                    value={settings.ixBrowserProxyHost}
                    onChange={(event) => handleSettingsChange('ixBrowserProxyHost', event.target.value)}
                  />
                  <p className="sd-farm-settings-hint">
                    Address written into ixBrowser profile proxy settings during sync (usually 127.0.0.1).
                  </p>
                </div>
                <div className="form-group">
                  <label htmlFor="sd-farm-ix-proxy-type">Proxy type for profiles</label>
                  <select
                    id="sd-farm-ix-proxy-type"
                    className="premium-input"
                    value={settings.ixBrowserProxyType}
                    onChange={(event) => handleSettingsChange(
                      'ixBrowserProxyType',
                      event.target.value === 'socks5' ? 'socks5' : 'http',
                    )}
                  >
                    <option value="http">HTTP</option>
                    <option value="socks5">SOCKS5</option>
                  </select>
                  <p className="sd-farm-settings-hint">
                    Matches the auth-routing listener used during sync (HTTP or SOCKS5 port).
                  </p>
                </div>
              </div>
              {!settings.ixBrowserOk && settings.ixBrowserError && (
                <p className="sd-farm-ix-error">{settings.ixBrowserError}</p>
              )}
              {!settings.ixBrowserOk && settings.ixBrowserHint && (
                <p className="sd-farm-settings-hint">{settings.ixBrowserHint}</p>
              )}
              {!settings.ixBrowserOk && settings.ixBrowserTriedUrls?.length > 0 && (
                <p className="sd-farm-settings-hint">
                  Tried: {settings.ixBrowserTriedUrls.join(', ')}
                </p>
              )}
              {!settings.ixBrowserOk
                && settings.ixBrowserRecommendedBase
                && settings.ixBrowserRecommendedBase !== settings.ixBrowserApiBase && (
                <div className="sd-farm-ix-detected">
                  <span>Suggested: {settings.ixBrowserRecommendedBase}</span>
                  <button type="button" className="btn-outline" onClick={useDetectedIxUrl}>
                    Use detected URL
                  </button>
                </div>
              )}
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
                disabled={Boolean(busy) || importing || !settingsDirty}
              >
                <span className="material-symbols-outlined">save</span>
                Save settings
              </button>
            </div>
          </div>
        )}
      </section>

      <section className="card sd-farm-controls">
        <div className="sd-farm-controls-row">
          <div className="search-box sd-farm-search">
            <span className="material-symbols-outlined">search</span>
            <textarea
              rows={3}
              placeholder="Search UID, name, OVPN… or paste one UID per line"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
          <div className="sd-farm-actions">
            <button type="button" className="btn-secondary" onClick={loadAccounts} disabled={Boolean(busy)}>
              <span className="material-symbols-outlined">refresh</span>
              Refresh
            </button>
            <button
              type="button"
              className="btn-outline"
              onClick={() => exportRoutes('json')}
              disabled={Boolean(busy) || selectedUids.length === 0}
            >
              <span className="material-symbols-outlined">download</span>
              Export selected routes
            </button>
            <button
              type="button"
              className="btn-outline"
              onClick={() => routeImportInputRef.current?.click()}
              disabled={Boolean(busy)}
            >
              <span className="material-symbols-outlined">upload</span>
              Import routes
            </button>
            <input
              ref={routeImportInputRef}
              type="file"
              accept=".json,.csv,.txt,application/json,text/csv,text/plain"
              className="sd-farm-folder-input"
              onChange={handleRouteImportFile}
            />
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
          {searchParsed.mode === 'bulkUid' && (
            <span className="sd-farm-bulk-search-hint">
              Matching {searchParsed.uids.size} UIDs · {filteredRows.length} found
            </span>
          )}
        </div>
        <div className="sd-farm-paths">
          <span>{payload?.dbPath || ''}</span>
          {settings.routeMapCount > 0 && (
            <span>{settings.routeMapCount} saved route mapping{settings.routeMapCount === 1 ? '' : 's'}</span>
          )}
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
                    <td className="text-mono">
                      {row.routeUsername || '-'}
                      {row.routeUsernameCustom ? <span className="sd-farm-route-custom" title="Imported route name"> *</span> : null}
                    </td>
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
