import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useToast } from '../components/ui/feedback-hooks.js';
import {
  findAccountsDbInDirectoryHandle,
  findAccountsDbInWebkitFiles,
  importAccountsFile,
  labelFromWebkitFile,
  supportsDirectoryPicker,
} from './sdFarmImport.js';
import { copyToClipboard } from '../utils/copyToClipboard.js';
import './SDFarm.css';

const filters = [
  { id: 'all', label: 'All' },
  { id: 'valid', label: 'Ready' },
  { id: 'missing', label: 'Missing browser' },
  { id: 'ovpn', label: 'OVPN issues' },
  { id: 'duplicate', label: 'Duplicates' },
];

const activeSyncStatuses = new Set(['queued', 'running']);

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

function cookiesLabel(value) {
  const text = String(value || '').trim();
  if (!text) return '-';
  if (text.includes('=') && !text.includes('\\') && !text.includes('/')) {
    return text.length > 36 ? `${text.slice(0, 36)}…` : text;
  }
  const leaf = text.replace(/\\/g, '/').split('/').pop();
  return leaf || text;
}

function parseSearchQuery(raw) {
  const lines = String(raw || '')
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
  if (lines.length > 1) return { mode: 'bulkUid', uids: new Set(lines) };
  return { mode: 'text', needle: (lines[0] || '').toLowerCase() };
}

function syncJobIsActive(job) {
  return Boolean(job && activeSyncStatuses.has(job.status));
}

function formatElapsed(startedAt, endedAt = '') {
  const started = Date.parse(startedAt || '');
  if (!Number.isFinite(started)) return '0s';
  const ended = endedAt ? Date.parse(endedAt) : Date.now();
  const seconds = Math.max(0, Math.floor((ended - started) / 1000));
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return minutes > 0 ? `${minutes}m ${remainder}s` : `${remainder}s`;
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
  const [categoryFilter, setCategoryFilter] = useState('all');
  const [query, setQuery] = useState('');
  const [selectedUids, setSelectedUids] = useState([]);
  const [syncResults, setSyncResults] = useState({});
  const [settings, setSettings] = useState(defaultSettings);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [importing, setImporting] = useState(false);
  const [ixTesting, setIxTesting] = useState(false);
  const [copiedToken, setCopiedToken] = useState('');
  const [activeSyncJob, setActiveSyncJob] = useState(null);
  const [syncJobHistory, setSyncJobHistory] = useState([]);
  const [activeSyncTargetUids, setActiveSyncTargetUids] = useState([]);

  const rows = useMemo(() => payload?.rows || [], [payload]);
  const validRows = useMemo(() => rows.filter((row) => row.valid), [rows]);
  const syncActive = syncJobIsActive(activeSyncJob);
  const activeSyncResults = useMemo(() => {
    const mapped = {};
    (activeSyncJob?.results || []).forEach((item) => {
      if (item?.uid) mapped[item.uid] = item;
    });
    return mapped;
  }, [activeSyncJob]);
  const displayedSyncResults = useMemo(() => ({
    ...syncResults,
    ...activeSyncResults,
  }), [activeSyncResults, syncResults]);
  const categoryOptions = useMemo(() => {
    if (Array.isArray(payload?.categoryOptions)) return payload.categoryOptions;
    return Array.from(
      new Set(rows.map((row) => String(row.category || '').trim()).filter(Boolean)),
    ).sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));
  }, [payload, rows]);

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

  useEffect(() => {
    if (categoryFilter !== 'all' && !categoryOptions.includes(categoryFilter)) {
      setCategoryFilter('all');
    }
  }, [categoryFilter, categoryOptions]);

  useEffect(() => {
    if (!syncActive || !activeSyncJob?.id) return undefined;
    let cancelled = false;
    let completionHandled = false;

    const mergeJobResults = (job) => {
      const nextResults = {};
      (job?.results || []).forEach((item) => {
        if (item?.uid) nextResults[item.uid] = item;
      });
      if (Object.keys(nextResults).length) {
        setSyncResults((current) => ({ ...current, ...nextResults }));
      }
    };

    const handleTerminalJob = async (job) => {
      if (completionHandled) return;
      completionHandled = true;
      mergeJobResults(job);
      setSyncJobHistory((current) => [job, ...current.filter((item) => item.id !== job.id)].slice(0, 5));
      setActiveSyncTargetUids([]);
      const hasFailures = Number(job.failed || 0) > 0;
      const isCancelled = job.status === 'cancelled';
      const isFailed = job.status === 'failed';
      toast({
        title: isCancelled ? 'Sync cancelled' : isFailed ? 'Sync failed' : hasFailures ? 'Sync completed with errors' : 'Sync completed',
        message: isFailed
          ? (job.error || 'The sync job failed.')
          : `${job.synced || 0} synced${hasFailures ? `, ${job.failed || 0} failed` : ''}${job.skipped ? `, ${job.skipped} skipped` : ''}.`,
        variant: isFailed ? 'danger' : hasFailures || isCancelled ? 'warning' : 'success',
      });
      await loadAccounts();
    };

    const pollJob = async () => {
      try {
        const res = await fetch(`/api/sd-farm/sync-jobs/${encodeURIComponent(activeSyncJob.id)}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to load sync progress');
        if (cancelled) return;
        const job = data.job || data;
        setActiveSyncJob(job);
        mergeJobResults(job);
        if (!syncJobIsActive(job)) {
          await handleTerminalJob(job);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err.message || 'Failed to load sync progress');
        }
      }
    };

    pollJob();
    const timer = window.setInterval(pollJob, 800);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeSyncJob?.id, loadAccounts, syncActive, toast]);

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
      if (categoryFilter !== 'all' && String(row.category || '').trim() !== categoryFilter) return false;
      if (searchParsed.mode === 'bulkUid') {
        return searchParsed.uids.has(String(row.uid || '').trim());
      }
      if (!searchParsed.needle) return true;
      return [
        row.uid,
        row.name,
        row.category,
        row.password,
        row.twoFa,
        row.cookies,
        row.openvpn,
        row.browserProfileName,
        row.routeUsername,
      ]
        .some((value) => String(value || '').toLowerCase().includes(searchParsed.needle));
    });
  }, [categoryFilter, filter, rows, searchParsed]);

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
    const targetUids = Array.from(new Set((uids || []).map((uid) => String(uid || '').trim()).filter(Boolean)));
    setError('');
    try {
      const res = await fetch('/api/sd-farm/sync-jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          uids: targetUids,
          proxyType: settings.ixBrowserProxyType,
        }),
      });
      const raw = await res.text();
      let data;
      try {
        data = raw ? JSON.parse(raw) : {};
      } catch {
        throw new Error(raw.slice(0, 160) || 'Sync start failed: server returned non-JSON response');
      }
      if (!res.ok) {
        if (res.status === 409 && data.job) {
          setActiveSyncJob(data.job);
          toast({
            title: 'Sync already running',
            message: 'Showing the active sync job.',
            variant: 'warning',
          });
          return;
        }
        throw new Error(data.error || 'Sync start failed');
      }
      const job = data.job || data;
      setActiveSyncJob(job);
      setActiveSyncTargetUids(targetUids);
      toast({
        title: 'Sync started',
        message: targetUids.length === 1 ? `Syncing ${targetUids[0]}.` : `Syncing ${targetUids.length} account${targetUids.length === 1 ? '' : 's'}.`,
        variant: 'info',
      });
    } catch (err) {
      setError(err.message || 'Sync start failed');
      toast({ title: 'Sync failed', message: err.message || 'Sync start failed', variant: 'danger' });
    }
  };

  const cancelSyncJob = async () => {
    if (!activeSyncJob?.id || !syncActive) return;
    setError('');
    try {
      const res = await fetch(`/api/sd-farm/sync-jobs/${encodeURIComponent(activeSyncJob.id)}/cancel`, {
        method: 'POST',
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Cancel failed');
      setActiveSyncJob(data.job || activeSyncJob);
      toast({ title: 'Cancelling sync', message: 'The sync will stop after the current account.', variant: 'warning' });
    } catch (err) {
      setError(err.message || 'Cancel failed');
      toast({ title: 'Cancel failed', message: err.message || 'Cancel failed', variant: 'danger' });
    }
  };

  const copyTableValue = async (value, token, label) => {
    const text = String(value || '').trim();
    if (!text) return;
    try {
      await copyToClipboard(text);
      setCopiedToken(token);
      window.setTimeout(() => {
        setCopiedToken((current) => (current === token ? '' : current));
      }, 1800);
      toast({ title: 'Copied', message: `${label} copied to clipboard.`, variant: 'success', duration: 1800 });
    } catch (err) {
      toast({ title: 'Copy failed', message: err?.message || `Could not copy ${label.toLowerCase()}.`, variant: 'danger' });
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

  const syncTotal = Number(activeSyncJob?.total || 0);
  const syncCompleted = Number(activeSyncJob?.completed || 0);
  const syncPercent = syncTotal > 0 ? Math.min(100, Math.round((syncCompleted / syncTotal) * 100)) : 0;
  const syncStatusLabel = activeSyncJob?.status === 'running'
    ? 'Syncing'
    : activeSyncJob?.status === 'queued'
      ? 'Queued'
      : activeSyncJob?.status === 'completed'
        ? Number(activeSyncJob?.failed || 0) > 0 ? 'Completed with errors' : 'Completed'
        : activeSyncJob?.status === 'cancelled'
          ? 'Cancelled'
          : activeSyncJob?.status === 'failed'
            ? 'Failed'
            : '';
  const recentSyncResults = (activeSyncJob?.results || []).slice(-6).reverse();

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
                  disabled={Boolean(busy) || importing || syncActive}
                >
                  <span className="material-symbols-outlined">folder_open</span>
                  Select folder
                </button>
                <button
                  type="button"
                  className="btn-outline sd-farm-browse-btn"
                  onClick={handlePickFolder}
                  disabled={Boolean(busy) || importing || syncActive || !settings.hasImportedDb}
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
                  disabled={Boolean(busy) || ixTesting || syncActive}
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
                disabled={Boolean(busy) || importing || syncActive || !settingsDirty}
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
              disabled={Boolean(busy) || syncActive}
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
            <button type="button" className="btn-outline" onClick={previewSync} disabled={Boolean(busy) || syncActive}>
              <span className="material-symbols-outlined">rule</span>
              Preview
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => syncAccounts(selectedUids)}
              disabled={Boolean(busy) || syncActive || selectedValidCount === 0}
            >
              <span className="material-symbols-outlined">{syncActive ? 'progress_activity' : 'sync'}</span>
              {syncActive ? 'Sync running' : 'Sync selected'}
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => syncAccounts(validRows.map((row) => row.uid))}
              disabled={Boolean(busy) || syncActive || validRows.length === 0}
            >
              <span className="material-symbols-outlined">{syncActive ? 'progress_activity' : 'done_all'}</span>
              {syncActive ? 'Sync running' : 'Sync all'}
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
          <select
            className="sd-farm-category-select"
            value={categoryFilter}
            onChange={(event) => setCategoryFilter(event.target.value)}
            disabled={categoryOptions.length === 0}
            aria-label="Filter by category"
          >
            <option value="all">All categories</option>
            {categoryOptions.map((category) => (
              <option key={category} value={category}>
                {category}
              </option>
            ))}
          </select>
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

      {activeSyncJob && (
        <section className={`card sd-farm-sync-panel sd-farm-sync-panel-${activeSyncJob.status || 'idle'}`}>
          <div className="sd-farm-sync-header">
            <div className="sd-farm-sync-title">
              <span className="material-symbols-outlined">
                {syncActive ? 'sync' : activeSyncJob.status === 'failed' ? 'error' : 'task_alt'}
              </span>
              <div>
                <h3>{syncStatusLabel || 'Sync status'}</h3>
                <p>
                  {syncActive && activeSyncJob.currentUid
                    ? `${activeSyncJob.currentName || 'Account'} (${activeSyncJob.currentUid})`
                    : `${syncCompleted} / ${syncTotal} account${syncTotal === 1 ? '' : 's'}`}
                </p>
              </div>
            </div>
            <div className="sd-farm-sync-actions">
              <span className="sd-farm-sync-elapsed">
                {formatElapsed(activeSyncJob.startedAt, activeSyncJob.finishedAt)}
              </span>
              {syncActive && (
                <button type="button" className="btn-outline" onClick={cancelSyncJob}>
                  <span className="material-symbols-outlined">stop_circle</span>
                  Cancel
                </button>
              )}
            </div>
          </div>
          <div className="sd-farm-sync-progress" aria-label={`Sync progress ${syncPercent}%`}>
            <span style={{ width: `${syncPercent}%` }} />
          </div>
          <div className="sd-farm-sync-stats">
            <span><strong>{syncCompleted}</strong> done</span>
            <span><strong>{activeSyncJob.synced || 0}</strong> synced</span>
            <span><strong>{activeSyncJob.failed || 0}</strong> failed</span>
            <span><strong>{activeSyncJob.skipped || 0}</strong> skipped</span>
            {activeSyncJob.proxyPort ? (
              <span>{String(activeSyncJob.proxyType || '').toUpperCase()} {activeSyncJob.proxyHost}:{activeSyncJob.proxyPort}</span>
            ) : null}
          </div>
          {activeSyncJob.error && <p className="sd-farm-sync-error">{activeSyncJob.error}</p>}
          {recentSyncResults.length > 0 && (
            <div className="sd-farm-sync-results">
              {recentSyncResults.map((item) => (
                <div key={`${item.uid}-${item.ok ? 'ok' : 'err'}`} className={`sd-farm-sync-result ${item.ok ? 'ok' : 'error'}`}>
                  <span className="material-symbols-outlined">{item.ok ? 'check_circle' : 'error'}</span>
                  <strong>{item.name || item.uid || 'Account'}</strong>
                  <small>{item.ok ? item.routeUsername || 'Synced' : item.error || 'Failed'}</small>
                </div>
              ))}
            </div>
          )}
          {!syncActive && syncJobHistory.length > 1 && (
            <p className="sd-farm-sync-history">
              Last sync kept here; {syncJobHistory.length - 1} earlier result{syncJobHistory.length === 2 ? '' : 's'} available in this session.
            </p>
          )}
        </section>
      )}

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
                <th>Category</th>
                <th>Password</th>
                <th>2FA</th>
                <th>Cookies</th>
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
                const activeResult = activeSyncResults[row.uid];
                const isSyncTarget = syncActive && activeSyncTargetUids.includes(row.uid);
                const isCurrentSyncRow = syncActive && activeSyncJob?.currentUid === row.uid;
                const result = isSyncTarget ? activeResult : displayedSyncResults[row.uid];
                const isPendingSyncRow = isSyncTarget && !activeResult && !isCurrentSyncRow;
                const badgeClass = isCurrentSyncRow || isPendingSyncRow
                  ? 'warning'
                  : result ? (result.ok ? 'success' : 'danger') : statusClass(row);
                const statusLabel = isCurrentSyncRow
                  ? 'Syncing'
                  : isPendingSyncRow
                    ? 'Pending'
                    : result ? (result.ok ? 'Synced' : 'Failed') : row.valid ? 'Ready' : 'Warning';
                const statusMessage = isCurrentSyncRow
                  ? 'Updating ixBrowser profile...'
                  : isPendingSyncRow
                    ? 'Waiting for sync'
                    : result?.error || rowIssue(row);
                return (
                  <tr
                    key={row.uid || `${row.name}-${row.openvpn}`}
                    className={isCurrentSyncRow ? 'sd-farm-row-syncing' : isPendingSyncRow ? 'sd-farm-row-pending' : ''}
                  >
                    <td className="sd-farm-check">
                      <input
                        type="checkbox"
                        checked={selectedUids.includes(row.uid)}
                        disabled={!row.valid}
                        onChange={() => toggleUid(row.uid)}
                        aria-label={`Select ${row.uid}`}
                      />
                    </td>
                    <td className="text-mono">
                      {row.uid ? (
                        <button
                          type="button"
                          className="sd-farm-copy-line text-mono"
                          title={`Click to copy: ${row.uid}`}
                          onClick={() => copyTableValue(row.uid, `uid-${row.uid}`, 'UID')}
                        >
                          <span className="sd-farm-copy-code">{row.uid}</span>
                          {copiedToken === `uid-${row.uid}` && (
                            <span className="sd-farm-copy-toast">Copied</span>
                          )}
                        </button>
                      ) : (
                        '-'
                      )}
                    </td>
                    <td>{row.name || '-'}</td>
                    <td>{row.category || '-'}</td>
                    <td className="text-mono">
                      {row.password ? (
                        <button
                          type="button"
                          className="sd-farm-copy-line text-mono"
                          title={`Click to copy: ${row.password}`}
                          onClick={() => copyTableValue(row.password, `password-${row.uid}`, 'Password')}
                        >
                          <span className="sd-farm-copy-code">{row.password}</span>
                          {copiedToken === `password-${row.uid}` && (
                            <span className="sd-farm-copy-toast">Copied</span>
                          )}
                        </button>
                      ) : (
                        '-'
                      )}
                    </td>
                    <td className="text-mono">
                      {row.twoFa ? (
                        <button
                          type="button"
                          className="sd-farm-copy-line text-mono"
                          title={`Click to copy: ${row.twoFa}`}
                          onClick={() => copyTableValue(row.twoFa, `twofa-${row.uid}`, '2FA')}
                        >
                          <span className="sd-farm-copy-code">{row.twoFa}</span>
                          {copiedToken === `twofa-${row.uid}` && (
                            <span className="sd-farm-copy-toast">Copied</span>
                          )}
                        </button>
                      ) : (
                        '-'
                      )}
                    </td>
                    <td>
                      {row.cookies ? (
                        <button
                          type="button"
                          className="sd-farm-copy-line text-mono"
                          title={`Click to copy: ${row.cookies}`}
                          onClick={() => copyTableValue(row.cookies, `cookies-${row.uid}`, 'Cookie')}
                        >
                          <span className="sd-farm-copy-code">{cookiesLabel(row.cookies)}</span>
                          {copiedToken === `cookies-${row.uid}` && (
                            <span className="sd-farm-copy-toast">Copied</span>
                          )}
                        </button>
                      ) : (
                        '-'
                      )}
                    </td>
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
                        {statusLabel}
                      </span>
                      <p className="sd-farm-issue">{statusMessage}</p>
                    </td>
                    <td className="sd-farm-action-col">
                      <button
                        type="button"
                        className="icon-btn"
                        title="Sync account"
                        onClick={() => syncAccounts([row.uid])}
                        disabled={!row.valid || Boolean(busy) || syncActive}
                      >
                        <span className="material-symbols-outlined">
                          {isCurrentSyncRow ? 'progress_activity' : 'sync'}
                        </span>
                      </button>
                    </td>
                  </tr>
                );
              })}
              {filteredRows.length === 0 && (
                <tr>
                  <td colSpan="13" className="sd-farm-empty">
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
