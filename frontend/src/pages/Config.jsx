import React, { useEffect, useState, useRef } from 'react';
import { publishedPortForIndex } from '../utils/portDisplay';
import './Config.css';

function normalizeRandomizeCountrySelect(v) {
  if (v == null || v === '') return 'random';
  const s = String(v).trim().toLowerCase();
  if (s === 'random') return 'random';
  const up = String(v).trim().toUpperCase();
  return /^[A-Z]{2}$/.test(up) ? up : 'random';
}

export default function Config() {
  const [config, setConfig] = useState(null);
  const [isDirty, setIsDirty] = useState(false);
  const [enabledPorts, setEnabledPorts] = useState(new Set());
  const [busyPort, setBusyPort] = useState(null);
  const [ovpnCountries, setOvpnCountries] = useState([]);
  const [ovpnScanMeta, setOvpnScanMeta] = useState({ count: 0, unclassified: 0 });
  /** Fields from /api/status for host port display and Docker publish alignment hints */
  const [statusPorts, setStatusPorts] = useState({
    portBase: null,
    publishedPortBase: null,
    dockerPublishedHostPortFirst: null,
    dockerPublishedHostPortLast: null,
    dockerPublishedPortSpan: null,
    gatewayListenerCount: null,
    publishMismatch: false,
    publishMismatchHint: '',
  });
  const fileInputRef = useRef(null);

  useEffect(() => {
    Promise.all([
      fetch('/api/config').then((res) => res.json()),
      fetch('/api/ovpn-files')
        .then((res) => res.json())
        .catch(() => ({ countries: [], ovpnCount: 0, unclassifiedOvpnCount: 0 })),
    ])
      .then(([data, ovpnPayload]) => {
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
        setIsDirty(false);
      })
      .catch((err) => console.error('Error fetching config:', err));
  }, []);

  useEffect(() => {
    const loadStatus = () => {
      fetch('/api/status')
        .then(res => res.json())
        .then(data => {
          const ports = Array.isArray(data.enabledPorts) ? data.enabledPorts : [];
          setEnabledPorts(new Set(ports));
          setStatusPorts({
            portBase: typeof data.portBase === 'number' ? data.portBase : null,
            publishedPortBase: typeof data.publishedPortBase === 'number' ? data.publishedPortBase : null,
            dockerPublishedHostPortFirst:
              typeof data.dockerPublishedHostPortFirst === 'number' ? data.dockerPublishedHostPortFirst : null,
            dockerPublishedHostPortLast:
              typeof data.dockerPublishedHostPortLast === 'number' ? data.dockerPublishedHostPortLast : null,
            dockerPublishedPortSpan:
              typeof data.dockerPublishedPortSpan === 'number' ? data.dockerPublishedPortSpan : null,
            gatewayListenerCount: typeof data.totalPorts === 'number' ? data.totalPorts : null,
            publishMismatch: !!data.publishMismatch,
            publishMismatchHint: typeof data.publishMismatchHint === 'string' ? data.publishMismatchHint : '',
          });
        })
        .catch(() => {
          // Status endpoint may be unavailable briefly during startup.
        });
    };
    loadStatus();
    const id = setInterval(loadStatus, 5000);
    return () => clearInterval(id);
  }, []);

  const handleChange = (field, value) => {
    setConfig(prev => ({ ...prev, [field]: value }));
    setIsDirty(true);
  };

  const handleLocationChange = (index, field, value) => {
    setConfig(prev => {
      const newLocs = [...prev.locations];
      newLocs[index] = { ...newLocs[index], [field]: value };
      return { ...prev, locations: newLocs };
    });
    setIsDirty(true);
  };

  const addLocation = () => {
    setConfig(prev => ({
      ...prev,
      locations: [...(prev.locations || []), { label: '', ovpn: '', username: '', password: '', randomAccess: false }]
    }));
    setIsDirty(true);
  };

  const removeLocation = (index) => {
    setConfig(prev => {
      const newLocs = [...prev.locations];
      newLocs.splice(index, 1);
      return { ...prev, locations: newLocs };
    });
    setIsDirty(true);
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
        setIsDirty(false);
        alert('Configuration saved! Please restart the gateway to apply changes.');
      } else {
        alert('Failed to save config: ' + data.error);
      }
    } catch (err) {
      alert('Error saving config: ' + err.message);
    }
  };

  const setPortActivation = async (port, enable) => {
    setBusyPort(port);
    try {
      const endpoint = enable ? '/api/activate' : '/api/deactivate';
      const res = await fetch(`${endpoint}?port=${encodeURIComponent(port)}`, {
        method: 'POST'
      });
      const data = await res.json();
      if (!data.ok) {
        alert((enable ? 'Activation failed: ' : 'Deactivation failed: ') + (data.error || 'Unknown error'));
        return;
      }
      setEnabledPorts(prev => {
        const next = new Set(prev);
        if (enable) {
          next.add(port);
        } else {
          next.delete(port);
        }
        return next;
      });
    } catch (err) {
      alert('Port activation request failed: ' + err.message);
    } finally {
      setBusyPort(null);
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
      } catch (err) {
        alert("Invalid JSON file");
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

  const portDisplayStatus = {
    portBase: statusPorts.portBase ?? config.portBase ?? 0,
    publishedPortBase: statusPorts.publishedPortBase,
  };
  const showHostPortCol =
    statusPorts.publishedPortBase != null && typeof statusPorts.publishedPortBase === 'number';
  const hostRangeSummary =
    statusPorts.dockerPublishedHostPortFirst != null && statusPorts.dockerPublishedHostPortLast != null
      ? `Docker publishes host TCP ${statusPorts.dockerPublishedHostPortFirst}–${statusPorts.dockerPublishedHostPortLast}` +
        (statusPorts.dockerPublishedPortSpan != null
          ? ` (${statusPorts.dockerPublishedPortSpan} slots). Align location count and portBase with compose/.env.`
          : '.')
      : null;

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
      </div>

      <section className="card p-0 overflow-hidden">
        <div className="table-header">
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined text-primary">public</span>
            <h3 className="font-bold">Proxy Locations</h3>
          </div>
          <button className="btn-primary-soft" onClick={addLocation}>
            <span className="material-symbols-outlined">add_location_alt</span>
            Add Location
          </button>
        </div>
        {statusPorts.publishMismatch && statusPorts.publishMismatchHint ? (
          <div className="config-publish-mismatch-banner" role="alert">
            <strong>Publish range mismatch.</strong> {statusPorts.publishMismatchHint}
          </div>
        ) : null}
        {hostRangeSummary ? (
          <p className="config-host-range-hint text-muted text-sm px-4 pt-3 mb-0">{hostRangeSummary}</p>
        ) : null}
        {statusPorts.gatewayListenerCount != null &&
          config.locations.length > 0 &&
          statusPorts.gatewayListenerCount > config.locations.length && (
            <p className="text-muted text-sm px-4 pt-2 mb-0" role="status">
              The gateway is listening on <strong>{statusPorts.gatewayListenerCount}</strong> port(s) (Docker publish
              span). This file only defines <strong>{config.locations.length}</strong> row(s); extra slots use{' '}
              <code className="text-xs">locationSpec.defaultOvpn</code> (or the first configured OVPN path) until you
              edit JSON. Use the <strong>Dashboard</strong> to pick any profile per port.
            </p>
          )}
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>Label</th>
                <th>Listener (container)</th>
                {showHostPortCol ? <th>Host port</th> : null}
                <th>OVPN Filename</th>
                <th>Username</th>
                <th>Password</th>
                <th>Random access</th>
                <th>Activation</th>
                <th className="text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {config.locations.map((loc, idx) => (
                <tr key={idx}>
                  <td>
                    <input 
                      type="text" 
                      className="premium-input seamless" 
                      value={loc.label || ''}
                      onChange={e => handleLocationChange(idx, 'label', e.target.value)}
                      placeholder="e.g. US East"
                    />
                  </td>
                  <td>
                    <code>{(config.portBase || 0) + idx}</code>
                  </td>
                  {showHostPortCol ? (
                    <td>
                      <code>{publishedPortForIndex(portDisplayStatus, idx) ?? '—'}</code>
                    </td>
                  ) : null}
                  <td>
                    <input 
                      type="text" 
                      className="premium-input seamless" 
                      value={loc.ovpn || ''}
                      onChange={e => handleLocationChange(idx, 'ovpn', e.target.value)}
                      placeholder="file.ovpn"
                    />
                  </td>
                  <td>
                    <input 
                      type="text" 
                      className="premium-input seamless" 
                      value={loc.username || ''}
                      onChange={e => handleLocationChange(idx, 'username', e.target.value)}
                      placeholder="Optional"
                    />
                  </td>
                  <td>
                    <input 
                      type="password" 
                      className="premium-input seamless" 
                      value={loc.password || ''}
                      onChange={e => handleLocationChange(idx, 'password', e.target.value)}
                      placeholder="********"
                    />
                  </td>
                  <td className="text-center">
                    <label className="checkbox-label" style={{ justifyContent: 'center' }}>
                      <input 
                        type="checkbox" 
                        checked={!!loc.randomAccess}
                        onChange={e => handleLocationChange(idx, 'randomAccess', e.target.checked)}
                      />
                      <span className="checkbox-custom"></span>
                    </label>
                  </td>
                  <td>
                    {(() => {
                      const port = (config.portBase || 0) + idx;
                      const isEnabled = enabledPorts.has(port);
                      return (
                        <button
                          className={isEnabled ? 'btn-outline' : 'btn-primary-soft'}
                          disabled={busyPort === port}
                          onClick={() => setPortActivation(port, !isEnabled)}
                          title={isEnabled ? 'Deactivate this port' : 'Activate this port'}
                        >
                          {busyPort === port ? 'Working...' : (isEnabled ? 'Deactivate' : 'Activate')}
                        </button>
                      );
                    })()}
                  </td>
                  <td className="text-right">
                    <button className="text-danger hover-glow p-1" onClick={() => removeLocation(idx)}>
                      <span className="material-symbols-outlined text-lg">delete</span>
                    </button>
                  </td>
                </tr>
              ))}
              {config.locations.length === 0 && (
                <tr>
                  <td colSpan={showHostPortCol ? 9 : 8} className="text-center p-6 text-muted">
                    No locations added yet.
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
