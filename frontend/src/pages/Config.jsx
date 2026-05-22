import React, { useEffect, useState, useRef } from 'react';
import { useConfirm, useToast } from '../components/ui/feedback-hooks.js';
import './Config.css';

function normalizeRandomizeCountrySelect(v) {
  if (v == null || v === '') return 'random';
  const s = String(v).trim().toLowerCase();
  if (s === 'random') return 'random';
  const up = String(v).trim().toUpperCase();
  return /^[A-Z]{2}$/.test(up) ? up : 'random';
}

export default function Config() {
  const confirmAction = useConfirm();
  const toast = useToast();
  const [config, setConfig] = useState(null);
  const [isDirty, setIsDirty] = useState(false);
  const [providerAuthRows, setProviderAuthRows] = useState([]);
  const [providerAuthBusy, setProviderAuthBusy] = useState(false);
  const [providerAuthError, setProviderAuthError] = useState('');
  const [ovpnCountries, setOvpnCountries] = useState([]);
  const [upstreamProxies, setUpstreamProxies] = useState([]);
  const [upstreamBusy, setUpstreamBusy] = useState(false);
  const [upstreamError, setUpstreamError] = useState('');
  const [upstreamImportLines, setUpstreamImportLines] = useState('');
  const [upstreamImportResults, setUpstreamImportResults] = useState([]);
  const [upstreamForm, setUpstreamForm] = useState({
    id: '',
    label: '',
    scheme: 'http',
    host: '',
    port: '',
    username: '',
    password: '',
  });
  const [ovpnScanMeta, setOvpnScanMeta] = useState({ count: 0, unclassified: 0 });
  const fileInputRef = useRef(null);

  useEffect(() => {
    Promise.all([
      fetch('/api/config').then((res) => res.json()),
      fetch('/api/ovpn-files')
        .then((res) => res.json())
        .catch(() => ({ countries: [], ovpnCount: 0, unclassifiedOvpnCount: 0 })),
      fetch('/api/provider-auth')
        .then((res) => res.json())
        .catch(() => ({ providers: [] })),
      fetch('/api/upstream-proxies')
        .then((res) => res.json())
        .catch(() => ({ proxies: [] })),
    ])
      .then(([data, ovpnPayload, providerAuthPayload, upstreamPayload]) => {
        if (!data.locations) data.locations = [];
        if (data.randomizeCountry == null || data.randomizeCountry === '') {
          data.randomizeCountry = 'random';
        }
        setConfig(data);
        setOvpnCountries(Array.isArray(ovpnPayload.countries) ? ovpnPayload.countries : []);
        setOvpnScanMeta({
          count: typeof ovpnPayload.ovpnCount === 'number' ? ovpnPayload.ovpnCount : 0,
          unclassified:
            typeof ovpnPayload.unclassifiedOvpnCount === 'number'
              ? ovpnPayload.unclassifiedOvpnCount
              : 0,
        });
        setProviderAuthRows(
          Array.isArray(providerAuthPayload.providers) ? providerAuthPayload.providers : [],
        );
        setUpstreamProxies(Array.isArray(upstreamPayload.proxies) ? upstreamPayload.proxies : []);
        setProviderAuthError('');
        setIsDirty(false);
      })
      .catch((err) => console.error('Error fetching config:', err));
  }, []);

  const handleChange = (field, value) => {
    setConfig(prev => ({ ...prev, [field]: value }));
    setIsDirty(true);
  };

  const handleProviderAuthChange = (index, field, value) => {
    setProviderAuthRows((prev) => {
      const next = [...prev];
      next[index] = { ...next[index], [field]: value };
      return next;
    });
    setProviderAuthError('');
    setIsDirty(true);
  };

  const saveProviderAuth = async () => {
    setProviderAuthBusy(true);
    setProviderAuthError('');
    try {
      const payload = {
        providers: providerAuthRows.map((row) => ({
          provider: row.provider || '',
          username: row.username || '',
          password: row.password || '',
        })),
      };
      const res = await fetch('/api/provider-auth', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!data.ok) {
        const firstErr = Array.isArray(data.results)
          ? (data.results.find((r) => r && r.ok === false)?.error || '')
          : '';
        throw new Error(firstErr || data.error || 'Failed to save provider auth files');
      }
      const refreshed = await fetch('/api/provider-auth').then((r) => r.json());
      setProviderAuthRows(Array.isArray(refreshed.providers) ? refreshed.providers : []);
    } finally {
      setProviderAuthBusy(false);
    }
  };

  const resetUpstreamForm = () => {
    setUpstreamForm({
      id: '',
      label: '',
      scheme: 'http',
      host: '',
      port: '',
      username: '',
      password: '',
    });
  };

  const refreshUpstreamProxies = async () => {
    const data = await fetch('/api/upstream-proxies').then((res) => res.json());
    setUpstreamProxies(Array.isArray(data.proxies) ? data.proxies : []);
  };

  const saveUpstreamProxy = async (e) => {
    e.preventDefault();
    setUpstreamBusy(true);
    setUpstreamError('');
    try {
      const payload = {
        label: upstreamForm.label,
        scheme: upstreamForm.scheme,
        host: upstreamForm.host,
        port: Number(upstreamForm.port),
        username: upstreamForm.username,
      };
      if (upstreamForm.id) payload.id = upstreamForm.id;
      if (upstreamForm.password) payload.password = upstreamForm.password;
      const res = await fetch('/api/upstream-proxy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'Failed to save upstream proxy');
      await refreshUpstreamProxies();
      toast({
        title: upstreamForm.id ? 'Proxy updated' : 'Proxy added',
        message: `${upstreamForm.label || upstreamForm.host} is ready to use.`,
        variant: 'success',
      });
      resetUpstreamForm();
    } catch (err) {
      setUpstreamError(err.message || 'Failed to save upstream proxy');
      toast({ title: 'Proxy save failed', message: err.message || 'Failed to save upstream proxy.', variant: 'danger' });
    } finally {
      setUpstreamBusy(false);
    }
  };

  const editUpstreamProxy = (proxy) => {
    setUpstreamError('');
    setUpstreamForm({
      id: proxy.id || '',
      label: proxy.label || '',
      scheme: proxy.scheme === 'socks5' ? 'socks5' : 'http',
      host: proxy.host || '',
      port: proxy.port == null ? '' : String(proxy.port),
      username: proxy.username || '',
      password: '',
    });
  };

  const deleteUpstreamProxy = async (proxy) => {
    const accepted = await confirmAction({
      title: 'Delete upstream proxy?',
      message: `Delete ${proxy.label || proxy.host}? This removes the saved upstream profile.`,
      confirmLabel: 'Delete proxy',
      variant: 'danger',
    });
    if (!accepted) return;
    setUpstreamBusy(true);
    setUpstreamError('');
    try {
      const res = await fetch(`/api/delete-upstream-proxy?id=${encodeURIComponent(proxy.id)}`, {
        method: 'POST',
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'Failed to delete upstream proxy');
      await refreshUpstreamProxies();
      if (upstreamForm.id === proxy.id) resetUpstreamForm();
      toast({ title: 'Proxy deleted', message: `${proxy.label || proxy.host} was removed.`, variant: 'success' });
    } catch (err) {
      setUpstreamError(err.message || 'Failed to delete upstream proxy');
      toast({ title: 'Delete failed', message: err.message || 'Failed to delete upstream proxy.', variant: 'danger' });
    } finally {
      setUpstreamBusy(false);
    }
  };

  const importUpstreamProxies = async () => {
    setUpstreamBusy(true);
    setUpstreamError('');
    setUpstreamImportResults([]);
    try {
      const res = await fetch('/api/import-upstream-proxies', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lines: upstreamImportLines }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to import upstream proxies');
      setUpstreamImportResults(Array.isArray(data.results) ? data.results : []);
      await refreshUpstreamProxies();
      if ((data.imported || 0) > 0) {
        setUpstreamImportLines('');
        toast({
          title: 'Import complete',
          message: `${data.imported} upstream ${data.imported === 1 ? 'proxy' : 'proxies'} imported.`,
          variant: 'success',
        });
      }
    } catch (err) {
      setUpstreamError(err.message || 'Failed to import upstream proxies');
      toast({ title: 'Import failed', message: err.message || 'Failed to import upstream proxies.', variant: 'danger' });
    } finally {
      setUpstreamBusy(false);
    }
  };

  const saveConfig = async () => {
    try {
      const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config)
      });
      const data = await res.json();
      if (data.ok) {
        try {
          await saveProviderAuth();
        } catch (authErr) {
          setProviderAuthError(authErr.message || 'Provider auth save failed');
          toast({
            title: 'Config saved with auth error',
            message: authErr.message || 'Provider auth files failed to save.',
            variant: 'warning',
          });
          return;
        }
        setIsDirty(false);
        toast({
          title: 'Configuration saved',
          message: 'Restart the gateway to apply changes.',
          variant: 'success',
        });
      } else {
        toast({ title: 'Save failed', message: data.error || 'The gateway rejected the config.', variant: 'danger' });
      }
    } catch (err) {
      toast({ title: 'Save failed', message: err.message, variant: 'danger' });
    }
  };

  const exportConfig = () => {
    const blob = new Blob([JSON.stringify(config, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'portico-config.json';
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleImport = (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      try {
        const imported = JSON.parse(ev.target.result);
        if (!imported.locations) imported.locations = [];
        setConfig(imported);
        setIsDirty(true);
      } catch {
        toast({ title: 'Invalid JSON file', message: 'The imported file could not be parsed.', variant: 'danger' });
      }
    };
    reader.readAsText(file);
    e.target.value = '';
  };

  if (!config) {
    return (
      <div className="loading-state">
        <span className="material-symbols-outlined loading-spinner">progress_activity</span>
        <p>Loading configuration...</p>
      </div>
    );
  }

  return (
    <div className="config-page">
      <div className="config-header-actions">
        <div className="flex items-center gap-4">
          <h2 className="title">Configuration</h2>
          {isDirty && (
            <span className="dirty-badge">
              <span className="dot animate-pulse"></span>
              Unsaved Changes
            </span>
          )}
        </div>
        
        <div className="flex items-center gap-3">
          <button className="btn-outline" onClick={exportConfig}>
            <span className="material-symbols-outlined">file_download</span>
            Export JSON
          </button>
          
          <button className="btn-outline" onClick={() => fileInputRef.current.click()}>
            <span className="material-symbols-outlined">file_upload</span>
            Import
          </button>
          <input 
            type="file" 
            ref={fileInputRef} 
            onChange={handleImport} 
            accept=".json,application/json" 
            style={{ display: 'none' }} 
          />
          
          <div className="divider"></div>
          
          <button className="btn-primary" onClick={saveConfig}>
            <span className="material-symbols-outlined">save</span>
            Save Configuration
          </button>
        </div>
      </div>

      <div className="config-grid">
        {/* Ports & Proxy */}
        <section className="card config-card">
          <div className="card-header">
            <span className="material-symbols-outlined text-primary">router</span>
            <h3 className="card-title">Ports & Proxy</h3>
          </div>
          <div className="form-group mb-4">
            <label>Port Base</label>
            <input 
              type="number" 
              className="premium-input" 
              value={config.portBase || 8080}
              onChange={e => handleChange('portBase', parseInt(e.target.value) || 0)}
            />
          </div>
          <div className="grid-2-col mb-4">
            <div className="form-group">
              <label>Proxy Username</label>
              <input 
                type="text" 
                className="premium-input" 
                placeholder="Optional"
                value={config.proxyUsername || ''}
                onChange={e => handleChange('proxyUsername', e.target.value)}
              />
            </div>
            <div className="form-group">
              <label>Proxy Password</label>
              <input 
                type="password" 
                className="premium-input" 
                value={config.proxyPassword || ''}
                onChange={e => handleChange('proxyPassword', e.target.value)}
              />
            </div>
          </div>
          <div className="form-group">
            <label>Proxy Listen Host</label>
            <input 
              type="text" 
              className="premium-input" 
              value={config.proxyListenHost || '0.0.0.0'}
              onChange={e => handleChange('proxyListenHost', e.target.value)}
            />
          </div>
          <div className="form-group">
            <label>Client proxy host</label>
            <input
              type="text"
              className="premium-input"
              placeholder="e.g. VPS public IP (empty = auto)"
              value={config.clientProxyHost ?? ''}
              onChange={(e) => handleChange('clientProxyHost', e.target.value)}
            />
            <p className="text-muted text-sm mt-1 mb-0">
              Hostname or IP shown in the dashboard for HTTP proxy URLs. Leave empty on a VPS to let the gateway
              detect your public IPv4 (cached; uses ifconfig.me / ipify with fallbacks). Set explicitly when you need
              a DNS name or a LAN IP instead of the detected WAN address.
            </p>
            <label className="checkbox-label mt-3">
              <input
                type="checkbox"
                checked={config.autoDetectClientProxyHost !== false}
                onChange={(e) => handleChange('autoDetectClientProxyHost', e.target.checked)}
              />
              <span>Auto-detect public IPv4 when client proxy host is empty and listeners use all interfaces</span>
            </label>
          </div>
        </section>

        {/* Gateway Settings */}
        <section className="card config-card">
          <div className="card-header">
            <span className="material-symbols-outlined text-primary">hub</span>
            <h3 className="card-title">Gateway Settings</h3>
          </div>
          <div className="form-group mb-4">
            <label>Internal Port Base</label>
            <input 
              type="number" 
              className="premium-input" 
              value={config.internalPortBase || 3000}
              onChange={e => handleChange('internalPortBase', parseInt(e.target.value) || 0)}
            />
          </div>
          <div className="grid-2-col mb-4">
            <div className="form-group">
              <label>Max Slots</label>
              <input 
                type="number" 
                className="premium-input" 
                value={config.maxSlots || 10}
                onChange={e => handleChange('maxSlots', parseInt(e.target.value) || 0)}
              />
            </div>
            <div className="form-group">
              <label>Idle Timeout (Min)</label>
              <input 
                type="number" 
                className="premium-input" 
                value={config.idleTimeoutMinutes || 45}
                onChange={e => handleChange('idleTimeoutMinutes', parseInt(e.target.value) || 0)}
              />
            </div>
          </div>
          <div className="form-group">
            <label>Control Port</label>
            <input 
              type="number" 
              className="premium-input" 
              value={config.controlPort || 9000}
              onChange={e => handleChange('controlPort', parseInt(e.target.value) || 0)}
            />
          </div>
          <label className="checkbox-label mt-3">
            <input
              type="checkbox"
              checked={config.autoActivateOnStartup !== false}
              onChange={e => handleChange('autoActivateOnStartup', e.target.checked)}
            />
            <span className="checkbox-custom"></span>
            Auto-activate persisted ports on gateway startup
          </label>
          <p className="text-muted text-sm mt-1 mb-0">
            When enabled, listener ports saved as active in{' '}
            <code className="text-mono">openvpn-proxy-assignments.json</code> are started again after a restart
            (OVPN picks and active set are both stored there).
          </p>
          <div className="form-group mt-4">
            <label htmlFor="config-randomize-country">Random pool country</label>
            <select
              id="config-randomize-country"
              className="premium-input"
              value={normalizeRandomizeCountrySelect(config.randomizeCountry)}
              onChange={(e) => handleChange('randomizeCountry', e.target.value)}
            >
              <option value="random">Random (any country)</option>
              {ovpnCountries.map((c) => (
                <option key={c.code} value={c.code}>
                  {c.label} ({c.count} profile{c.count === 1 ? '' : 's'})
                </option>
              ))}
              {(() => {
                const rc = normalizeRandomizeCountrySelect(config.randomizeCountry);
                if (rc === 'random' || ovpnCountries.some((c) => c.code === rc)) return null;
                return (
                  <option value={rc}>
                    {rc} (not in current OVPN scan)
                  </option>
                );
              })()}
            </select>
            <p className="text-muted text-sm mt-1 mb-0">
              Restricts the Dashboard <strong>Random</strong> button to profiles inferred from filenames in your
              OVPN folder (Proton-style, <code className="text-mono">United_States_…</code>, or{' '}
              <code className="text-mono">xx_city.ovpn</code>). Manual profile selection is unchanged. This takes effect
              on the next randomize without restarting the gateway.
            </p>
            {ovpnScanMeta.count === 0 && (
              <p className="text-muted text-sm mt-2 mb-0">
                <strong>0 profiles</strong> visible to the gateway at the configured OVPN path (or Docker{' '}
                <code className="text-mono">/ovpn</code> mount). Country rows still appear for selection; add{' '}
                <code className="text-mono">.ovpn</code> files there and refresh this page — or fix{' '}
                <code className="text-mono">OVPN_HOST_PATH</code> / <code className="text-mono">ovpnRoot</code> if the
                folder is wrong.
              </p>
            )}
            {ovpnScanMeta.count > 0 && ovpnScanMeta.unclassified === ovpnScanMeta.count && (
              <p className="text-muted text-sm mt-2 mb-0">
                The gateway sees <strong>{ovpnScanMeta.count}</strong> <code className="text-mono">.ovpn</code> file
                {ovpnScanMeta.count === 1 ? '' : 's'}, but <strong>none</strong> match a country pattern, so every
                country shows <strong>0 profiles</strong>. Rename files to Proton style,{' '}
                <code className="text-mono">United_States_State_City.ovpn</code>, or{' '}
                <code className="text-mono">de_city.ovpn</code>.
              </p>
            )}
            {ovpnScanMeta.count > 0 &&
              ovpnScanMeta.unclassified > 0 &&
              ovpnScanMeta.unclassified < ovpnScanMeta.count && (
                <p className="text-muted text-sm mt-2 mb-0">
                  <strong>{ovpnScanMeta.unclassified}</strong> file
                  {ovpnScanMeta.unclassified === 1 ? '' : 's'} in the OVPN folder could not be assigned a country from
                  the filename (those profiles are only used for <strong>Random (any country)</strong> or manual picks).
                </p>
              )}
          </div>
        </section>

        {/* OpenVPN & Docker */}
        <section className="card config-card col-span-2">
          <div className="card-header">
            <span className="material-symbols-outlined text-primary">box</span>
            <h3 className="card-title">OpenVPN & Docker</h3>
          </div>
          <div className="grid-2-col gap-6">
            <div className="form-group">
              <label>OVPN Root Directory</label>
              <input 
                type="text" 
                className="premium-input" 
                value={config.ovpnRoot || ''}
                onChange={e => handleChange('ovpnRoot', e.target.value)}
              />
            </div>
            <div className="form-group">
              <label>OpenVPN Binary Path</label>
              <input 
                type="text" 
                className="premium-input" 
                value={config.openvpnPath || ''}
                onChange={e => handleChange('openvpnPath', e.target.value)}
              />
            </div>
          </div>
          <div className="form-group mt-4 mb-4">
            <label>Force Bind IP Path</label>
            <input 
              type="text" 
              className="premium-input" 
              value={config.forceBindIPPath || ''}
              onChange={e => handleChange('forceBindIPPath', e.target.value)}
            />
          </div>
          
          <div className="flex gap-6 mb-4">
            <label className="checkbox-label">
              <input 
                type="checkbox" 
                checked={config.useDocker || false}
                onChange={e => handleChange('useDocker', e.target.checked)}
              />
              <span className="checkbox-custom"></span>
              Use Docker Isolation
            </label>
            <label className="checkbox-label">
              <input 
                type="checkbox" 
                checked={config.saveRunFile || false}
                onChange={e => handleChange('saveRunFile', e.target.checked)}
              />
              <span className="checkbox-custom"></span>
              Save state to run file
            </label>
          </div>

          {config.useDocker && (
            <div className="docker-settings p-4 glass-panel border-l-primary">
              <div className="form-group mb-4">
                <label>Docker Image</label>
                <input 
                  type="text" 
                  className="premium-input" 
                  value={config.dockerImage || ''}
                  onChange={e => handleChange('dockerImage', e.target.value)}
                />
              </div>
              <div className="grid-2-col gap-4">
                <div className="form-group">
                  <label>Network</label>
                  <input 
                    type="text" 
                    className="premium-input" 
                    value={config.dockerNetwork || ''}
                    onChange={e => handleChange('dockerNetwork', e.target.value)}
                  />
                </div>
                <div className="form-group">
                  <label>OVPN Volume</label>
                  <input 
                    type="text" 
                    className="premium-input" 
                    value={config.dockerOvpnVolume || ''}
                    onChange={e => handleChange('dockerOvpnVolume', e.target.value)}
                  />
                </div>
              </div>
            </div>
          )}
        </section>

        <section className="card config-card col-span-2">
          <div className="card-header">
            <span className="material-symbols-outlined text-primary">vpn_key</span>
            <h3 className="card-title">VPN Provider Auth Files</h3>
          </div>
          <p className="text-muted text-sm mt-1 mb-3">
            Edit username/password per provider folder. Saving writes directly to each provider
            <code className="text-mono"> auth.txt </code>
            file under OVPN root.
          </p>
          {providerAuthError ? (
            <div className="config-publish-mismatch-banner" role="alert">
              <strong>Provider auth save failed.</strong> {providerAuthError}
            </div>
          ) : null}
          <div className="table-container">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Provider</th>
                  <th>Auth file</th>
                  <th>Username</th>
                  <th>Password</th>
                </tr>
              </thead>
              <tbody>
                {providerAuthRows.map((row, idx) => (
                  <tr key={row.provider || idx}>
                    <td data-label="Provider">
                      <code>{row.provider || '—'}</code>
                    </td>
                    <td data-label="Auth file">
                      <code>{row.authPath || ''}</code>
                    </td>
                    <td data-label="Username">
                      <input
                        type="text"
                        className="premium-input seamless"
                        value={row.username || ''}
                        onChange={(e) => handleProviderAuthChange(idx, 'username', e.target.value)}
                        placeholder="Provider username"
                        disabled={providerAuthBusy}
                      />
                    </td>
                    <td data-label="Password">
                      <input
                        type="password"
                        className="premium-input seamless"
                        value={row.password || ''}
                        onChange={(e) => handleProviderAuthChange(idx, 'password', e.target.value)}
                        placeholder="Provider password"
                        disabled={providerAuthBusy}
                      />
                    </td>
                  </tr>
                ))}
                {providerAuthRows.length === 0 && (
                  <tr>
                    <td colSpan={4} className="text-center p-6 text-muted">
                      No provider folders detected under current OVPN root.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="card config-card col-span-2">
          <div className="card-header">
            <span className="material-symbols-outlined text-primary">hub</span>
            <h3 className="card-title">Upstream Proxies</h3>
          </div>
          {upstreamError && (
            <div className="config-publish-mismatch-banner" role="alert">
              <strong>Upstream proxy action failed.</strong> {upstreamError}
            </div>
          )}
          <div className="upstream-proxy-layout">
            <form className="upstream-proxy-form" onSubmit={saveUpstreamProxy}>
              <div className="grid-2-col gap-4">
                <div className="form-group">
                  <label>Label</label>
                  <input
                    type="text"
                    className="premium-input"
                    value={upstreamForm.label}
                    onChange={(e) => setUpstreamForm((prev) => ({ ...prev, label: e.target.value }))}
                    placeholder="US residential 1"
                  />
                </div>
                <div className="form-group">
                  <label>Protocol</label>
                  <select
                    className="premium-input"
                    value={upstreamForm.scheme}
                    onChange={(e) =>
                      setUpstreamForm((prev) => ({
                        ...prev,
                        scheme: e.target.value === 'socks5' ? 'socks5' : 'http',
                      }))
                    }
                  >
                    <option value="http">HTTP</option>
                    <option value="socks5">SOCKS5</option>
                  </select>
                </div>
              </div>
              <div className="grid-2-col gap-4">
                <div className="form-group">
                  <label>Host</label>
                  <input
                    type="text"
                    className="premium-input"
                    value={upstreamForm.host}
                    onChange={(e) => setUpstreamForm((prev) => ({ ...prev, host: e.target.value }))}
                    placeholder="proxy.example.com"
                    required
                  />
                </div>
                <div className="form-group">
                  <label>Port</label>
                  <input
                    type="number"
                    className="premium-input"
                    min="1"
                    max="65535"
                    value={upstreamForm.port}
                    onChange={(e) => setUpstreamForm((prev) => ({ ...prev, port: e.target.value }))}
                    required
                  />
                </div>
              </div>
              <div className="grid-2-col gap-4">
                <div className="form-group">
                  <label>Username</label>
                  <input
                    type="text"
                    className="premium-input"
                    value={upstreamForm.username}
                    onChange={(e) => setUpstreamForm((prev) => ({ ...prev, username: e.target.value }))}
                  />
                </div>
                <div className="form-group">
                  <label>Password</label>
                  <input
                    type="password"
                    className="premium-input"
                    value={upstreamForm.password}
                    onChange={(e) => setUpstreamForm((prev) => ({ ...prev, password: e.target.value }))}
                    placeholder={upstreamForm.id ? 'Leave blank to keep saved password' : ''}
                  />
                </div>
              </div>
              <div className="flex gap-3">
                <button className="btn-primary" type="submit" disabled={upstreamBusy}>
                  <span className="material-symbols-outlined">save</span>
                  {upstreamForm.id ? 'Update Proxy' : 'Add Proxy'}
                </button>
                {upstreamForm.id && (
                  <button className="btn-outline" type="button" onClick={resetUpstreamForm}>
                    Cancel Edit
                  </button>
                )}
              </div>
            </form>

            <div className="upstream-proxy-import">
              <label className="form-group">
                <span>Bulk Paste</span>
                <textarea
                  className="premium-input"
                  rows={7}
                  value={upstreamImportLines}
                  onChange={(e) => setUpstreamImportLines(e.target.value)}
                  placeholder={'host:port\nhost:port:user:pass\nhttp://user:pass@host:port'}
                />
              </label>
              <button
                className="btn-outline"
                type="button"
                onClick={importUpstreamProxies}
                disabled={upstreamBusy || !upstreamImportLines.trim()}
              >
                <span className="material-symbols-outlined">playlist_add</span>
                Import Lines
              </button>
              {upstreamImportResults.length > 0 && (
                <div className="upstream-import-results">
                  {upstreamImportResults.map((row) => (
                    <p key={`${row.line}-${row.ok ? 'ok' : 'err'}`} className={row.ok ? 'text-success' : 'text-muted'}>
                      Line {row.line}: {row.ok ? row.proxy?.label || 'Imported' : row.error}
                    </p>
                  ))}
                </div>
              )}
            </div>
          </div>
          <div className="table-container mt-4">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Label</th>
                  <th>Upstream</th>
                  <th>Username</th>
                  <th>Password</th>
                  <th className="text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {upstreamProxies.map((proxy) => (
                  <tr key={proxy.id}>
                    <td className="font-medium" data-label="Label">{proxy.label}</td>
                    <td className="text-mono" data-label="Upstream">
                      {proxy.scheme}://{proxy.host}:{proxy.port}
                    </td>
                    <td className="text-mono" data-label="Username">{proxy.username || '—'}</td>
                    <td data-label="Password">{proxy.hasPassword ? 'Saved' : '—'}</td>
                    <td className="text-right" data-label="Actions">
                      <div className="flex gap-2 justify-end">
                        <button className="btn-secondary" type="button" onClick={() => editUpstreamProxy(proxy)}>
                          Edit
                        </button>
                        <button className="btn-danger" type="button" onClick={() => deleteUpstreamProxy(proxy)}>
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
                {upstreamProxies.length === 0 && (
                  <tr>
                    <td colSpan={5} className="text-center p-6 text-muted">
                      No upstream proxies saved yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      </div>


    </div>
  );
}
