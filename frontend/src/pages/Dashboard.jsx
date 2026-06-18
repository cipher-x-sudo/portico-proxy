import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useConfirm, useToast } from '../components/ui/feedback-hooks.js';
import './Dashboard.css';
import OvpnFileSelect from '../components/OvpnFileSelect';
import { formatOvpnDisplayLabel, sortOvpnFiles } from '../utils/ovpnFiles';
import { copyToClipboard } from '../utils/copyToClipboard';
import { internalPortForIndex, internalToPublishedPort, publishedPortForIndex } from '../utils/portDisplay';

const randomProxySuffix = () => Math.random().toString(16).slice(2, 6).padEnd(4, '0');

const providerFromOvpn = (filename) => {
  const parts = String(filename || '').split(/[\\/]+/).filter(Boolean);
  return parts.length > 1 ? parts[0] : 'Uploaded';
};

const locationFromOvpn = (filename) => {
  const parts = String(filename || '').split(/[\\/]+/).filter(Boolean);
  const leaf = parts.length ? parts[parts.length - 1] : filename;
  return formatOvpnDisplayLabel(leaf || filename);
};

const slugifyProxyUsername = (value) => {
  const slug = String(value || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .replace(/_+/g, '_')
    .slice(0, 32)
    .replace(/^_+|_+$/g, '');
  return slug || 'proxy';
};

const authRoutePayloadFromRoute = (route, overrides = {}) => {
  const egress = route.egress || {};
  return {
    username: route.username || '',
    label: route.label || route.username || '',
    externalId: route.externalId || '',
    proxyType: route.proxyType === 'socks5' ? 'socks5' : 'http',
    enabled: route.enabled !== false,
    rotationIntervalMinutes: Math.max(0, Math.floor(Number(route.rotationIntervalMinutes) || 0)),
    rotationCountry: (route.rotationCountry || '').toUpperCase(),
    egress:
      egress.type === 'upstream'
        ? { type: 'upstream', upstreamProxyId: egress.upstreamProxyId || '' }
        : { type: 'ovpn', ovpn: egress.type === 'ovpn' ? egress.ovpn || '' : '' },
    ...overrides,
  };
};

const isSdFarmAuthRoute = (route) => String(route?.username || '').startsWith('sd_');

export default function Dashboard() {
  const confirmAction = useConfirm();
  const toast = useToast();
  const [status, setStatus] = useState(null);
  const [ovpnFiles, setOvpnFiles] = useState([]);
  const [ovpnFilesHint, setOvpnFilesHint] = useState('');
  const [ovpnCountries, setOvpnCountries] = useState([]);
  const [upstreamProxies, setUpstreamProxies] = useState([]);
  const [selectedByPort, setSelectedByPort] = useState({});
  const [busyPort, setBusyPort] = useState(null);
  const [batchBusy, setBatchBusy] = useState(false);
  const [selectedTablePorts, setSelectedTablePorts] = useState([]);
  const [launcherIdFilter, setLauncherIdFilter] = useState('');
  /** Ports Launcher table: null = preserve filter order (by port index). */
  const [launcherTableSort, setLauncherTableSort] = useState({ key: null, dir: 'asc' });
  const [error, setError] = useState('');
  const [copiedToken, setCopiedToken] = useState(null);
  const [authRouteBusy, setAuthRouteBusy] = useState(null);
  const [authRouteForm, setAuthRouteForm] = useState({
    username: '',
    label: '',
    externalId: '',
    proxyType: 'http',
    enabled: true,
    egressType: 'ovpn',
    ovpn: '',
    upstreamProxyId: '',
    rotationMinutes: '0',
    rotationCountry: '',
  });
  const [showAuthRouteEditor, setShowAuthRouteEditor] = useState(false);
  const authRoutesImportRef = useRef(null);
  const [authRoutesImportBusy, setAuthRoutesImportBusy] = useState(false);
  const [selectedAuthRoutes, setSelectedAuthRoutes] = useState([]);
  const [authRoutesBatchBusy, setAuthRoutesBatchBusy] = useState(false);
  const [authRouteSearch, setAuthRouteSearch] = useState('');
  const [authRouteTypeFilter, setAuthRouteTypeFilter] = useState('all');
  const [sdFarmProxyType, setSdFarmProxyType] = useState('http');
  const [sdFarmProxyTypeSaving, setSdFarmProxyTypeSaving] = useState(false);
  const [showCreateProxy, setShowCreateProxy] = useState(false);
  const [createProxyEgressType, setCreateProxyEgressType] = useState('ovpn');
  const [createProxyProvider, setCreateProxyProvider] = useState('');
  const [createProxyLocation, setCreateProxyLocation] = useState('');
  const [createProxyOvpn, setCreateProxyOvpn] = useState('');
  const [createProxyUpstreamProxyId, setCreateProxyUpstreamProxyId] = useState('');
  const [createProxyLabel, setCreateProxyLabel] = useState('');
  const [createProxyExternalId, setCreateProxyExternalId] = useState('');
  const [createProxyType, setCreateProxyType] = useState('http');
  const [createProxyRotationMinutes, setCreateProxyRotationMinutes] = useState('0');
  const [createProxyRotationCountry, setCreateProxyRotationCountry] = useState('');
  const [createProxySuffix, setCreateProxySuffix] = useState(randomProxySuffix);
  const [creatingProxy, setCreatingProxy] = useState(false);
  const [createdProxy, setCreatedProxy] = useState(null);

  const [showCreateEntry, setShowCreateEntry] = useState(false);
  const [newEntryId, setNewEntryId] = useState('');
  const [newEntryOvpn, setNewEntryOvpn] = useState('');
  const [newEntryEgressType, setNewEntryEgressType] = useState('ovpn');
  const [newEntryUpstreamProxyId, setNewEntryUpstreamProxyId] = useState('');
  const [newEntryProxyType, setNewEntryProxyType] = useState('http');
  const [newEntryUpstreamRefreshMinutes, setNewEntryUpstreamRefreshMinutes] = useState('0');
  const [creatingEntry, setCreatingEntry] = useState(false);

  const [showEditEntry, setShowEditEntry] = useState(false);
  const [editTargetPort, setEditTargetPort] = useState(null);
  const [editEntryId, setEditEntryId] = useState('');
  const [editEntryOvpn, setEditEntryOvpn] = useState('');
  const [editEntryEgressType, setEditEntryEgressType] = useState('ovpn');
  const [editEntryUpstreamProxyId, setEditEntryUpstreamProxyId] = useState('');
  const [editEntryProxyType, setEditEntryProxyType] = useState('http');
  const [editProxyTypeInitial, setEditProxyTypeInitial] = useState('http');
  const [editEntryUpstreamRefreshMinutes, setEditEntryUpstreamRefreshMinutes] = useState('0');
  const [editUpstreamRefreshInitial, setEditUpstreamRefreshInitial] = useState(0);
  const [isEditingEntry, setIsEditingEntry] = useState(false);

  useEffect(() => {
    const loadStatus = () => {
      fetch('/api/status')
        .then(res => res.json())
        .then(data => {
          setStatus(data);
          // Server is source of truth (include every listener port, often ""). Do not merge prev on top; that
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
          setOvpnCountries(Array.isArray(data.countries) ? data.countries : []);
        })
        .catch(err => console.error("Error fetching ovpn files:", err));
    };
    const loadUpstreamProxies = () => {
      fetch('/api/upstream-proxies')
        .then(res => res.json())
        .then(data => setUpstreamProxies(Array.isArray(data.proxies) ? data.proxies : []))
        .catch(err => console.error("Error fetching upstream proxies:", err));
    };
    loadStatus();
    loadFiles();
    loadUpstreamProxies();
    // Keep selected OVPN display in sync with auto-rotation quickly.
    const id = setInterval(loadStatus, 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (!status?.authRouting?.enabled) return undefined;
    fetch('/api/sd-farm/settings')
      .then((res) => res.json())
      .then((data) => {
        setSdFarmProxyType(data.ixBrowserProxyType === 'socks5' ? 'socks5' : 'http');
      })
      .catch(() => {});
    return undefined;
  }, [status?.authRouting?.enabled]);

  useEffect(() => {
    if (!status?.authRouting?.enabled) return;
    const usernames = new Set(
      (Array.isArray(status.authRouting.routes) ? status.authRouting.routes : []).map((route) => route.username),
    );
    setSelectedAuthRoutes((current) => current.filter((username) => usernames.has(username)));
  }, [status?.authRouting?.enabled, status?.authRouting?.routes]);

  useEffect(() => {
    const modalOpen = showCreateEntry || showEditEntry || showCreateProxy || showAuthRouteEditor;
    const page = document.querySelector('.main-content .page-container');
    if (!page) return undefined;
    if (!modalOpen) return undefined;
    const prevOverflow = page.style.overflow;
    page.style.overflow = 'hidden';
    return () => {
      page.style.overflow = prevOverflow;
    };
  }, [showCreateEntry, showEditEntry, showCreateProxy, showAuthRouteEditor]);

  const sortedOvpnFiles = useMemo(() => sortOvpnFiles(ovpnFiles), [ovpnFiles]);
  const ovpnRouteRows = useMemo(
    () =>
      sortedOvpnFiles.map((file) => ({
        file,
        provider: providerFromOvpn(file),
        location: locationFromOvpn(file),
      })),
    [sortedOvpnFiles]
  );

  const authRoutesList = useMemo(() => {
    if (!status?.authRouting?.enabled) return [];
    return Array.isArray(status.authRouting.routes) ? status.authRouting.routes : [];
  }, [status?.authRouting?.enabled, status?.authRouting?.routes]);

  const filteredAuthRoutes = useMemo(() => {
    const needle = authRouteSearch.trim().toLowerCase();
    return authRoutesList.filter((route) => {
      const routeType = route.proxyType === 'socks5' ? 'socks5' : 'http';
      if (authRouteTypeFilter === 'http' && routeType !== 'http') return false;
      if (authRouteTypeFilter === 'socks5' && routeType !== 'socks5') return false;
      if (authRouteTypeFilter === 'sd_farm' && !isSdFarmAuthRoute(route)) return false;
      if (!needle) return true;
      return [route.username, route.label, route.externalId]
        .some((value) => String(value || '').toLowerCase().includes(needle));
    });
  }, [authRouteSearch, authRouteTypeFilter, authRoutesList]);

  const visibleAuthRouteUsernames = useMemo(
    () => filteredAuthRoutes.map((route) => route.username).filter(Boolean),
    [filteredAuthRoutes],
  );

  const selectedAuthRoutesInView = useMemo(
    () => selectedAuthRoutes.filter((username) => visibleAuthRouteUsernames.includes(username)),
    [selectedAuthRoutes, visibleAuthRouteUsernames],
  );

  const allVisibleAuthRoutesSelected =
    visibleAuthRouteUsernames.length > 0 &&
    selectedAuthRoutesInView.length === visibleAuthRouteUsernames.length;

  const saveEgress = async (port, type, { ovpn = '', upstreamProxyId = '' } = {}) => {
    setBusyPort(port);
    setError('');
    const payload =
      type === 'upstream'
        ? { type: 'upstream', upstreamProxyId }
        : type === 'ovpn' && ovpn
          ? { type: 'ovpn', ovpn }
          : { type: 'none' };
    try {
      const res = await fetch(`/api/set-egress?port=${encodeURIComponent(port)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!data.ok) {
        setError(data.error || 'Failed to save egress');
        return false;
      }
      const refreshed = await fetch('/api/status').then((r) => r.json());
      setStatus(refreshed);
      setSelectedByPort(refreshed.assignedOvpnByPort || {});
      return true;
    } catch (err) {
      setError('Failed to save egress: ' + err.message);
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

  const saveProxyType = async (port, nextType, previousFromServer) => {
    const next = nextType === 'socks5' ? 'socks5' : 'http';
    const prev = previousFromServer === 'socks5' ? 'socks5' : 'http';
    if (next === prev) {
      return true;
    }
    setError('');
    try {
      const res = await fetch(`/api/set-proxy-type?port=${encodeURIComponent(port)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ proxyType: next }),
      });
      const data = await res.json();
      if (!data.ok) {
        setError(data.error || 'Failed to save proxy type');
        return false;
      }
      const refreshed = await fetch('/api/status').then((r) => r.json());
      setStatus(refreshed);
      setSelectedByPort(refreshed.assignedOvpnByPort || {});
      return true;
    } catch (err) {
      setError('Failed to save proxy type: ' + err.message);
      return false;
    }
  };

  const saveUpstreamRefresh = async (port, minutes) => {
    const intervalMinutes = Math.max(0, Math.floor(Number(minutes) || 0));
    setError('');
    try {
      const res = await fetch(`/api/set-upstream-refresh?port=${encodeURIComponent(port)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ intervalMinutes }),
      });
      const data = await res.json();
      if (!data.ok) {
        setError(data.error || 'Failed to save upstream refresh');
        return false;
      }
      return true;
    } catch (err) {
      setError('Failed to save upstream refresh: ' + err.message);
      return false;
    }
  };

  const changePortLocation = async (port, { ovpn = '', country = '' } = {}) => {
    setBusyPort(port);
    setError('');
    try {
      const payload = ovpn ? { ovpn } : { country };
      const res = await fetch(`/api/change-port-location?port=${encodeURIComponent(port)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!data.ok) {
        setError(data.error || 'Failed to change location');
        return false;
      }
      const refreshed = await fetch('/api/status').then((r) => r.json());
      setStatus(refreshed);
      setSelectedByPort(refreshed.assignedOvpnByPort || {});
      toast({
        title: 'Location changed',
        message: data.activationState === 'starting'
          ? 'The proxy is restarting on the same port.'
          : 'The new location is saved for this port.',
        variant: 'success',
      });
      return true;
    } catch (err) {
      setError('Failed to change location: ' + err.message);
      return false;
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

  /** Reload the backend while keeping the same egress assignment. */
  const restartPort = async (port, { hasEgress }) => {
    if (!hasEgress) {
      setError('Assign egress before restart.');
      return;
    }
    setBusyPort(port);
    setError('');
    try {
      const stopRes = await fetch(`/api/deactivate?port=${encodeURIComponent(port)}`, { method: 'POST' });
      const stopData = await stopRes.json();
      if (!stopData.ok) {
        setError(stopData.error || 'Failed to stop for restart');
        return;
      }
      const startRes = await fetch(`/api/activate?port=${encodeURIComponent(port)}`, { method: 'POST' });
      const startData = await startRes.json();
      if (!startData.ok) {
        setError(startData.error || 'Failed to start after restart');
        return;
      }
      const refreshed = await fetch('/api/status').then((r) => r.json());
      setStatus(refreshed);
      setSelectedByPort(refreshed.assignedOvpnByPort || {});
    } catch (err) {
      setError('Restart failed: ' + err.message);
    } finally {
      setBusyPort(null);
    }
  };

  const onSelectRowFile = async (port, ovpn) => {
    if (ovpn) {
      await changePortLocation(port, { ovpn });
    } else {
      await saveEgress(port, 'none');
    }
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
      const myEgressByPort = status.egressByPort || {};

      let unusedIdxs = [];
      for (let idx = 0; idx < totalP; idx++) {
        const loc = (status.locations || [])[idx] || {};
        const port = internalPortForIndex(status, idx);
        const portKey = String(port);
        const hasLauncherId = typeof loc.launcherId === 'string' && loc.launcherId.trim() !== '';
        const hasEgress =
          (myEgressByPort[portKey]?.type && myEgressByPort[portKey].type !== 'none') ||
          !!mySelectedByPort[portKey];
        const isEnabled = enabledPortsSet.has(port);
        if (!hasLauncherId && !hasEgress && !isEnabled) {
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

        const proxyOk = await saveProxyType(targetPort, newEntryProxyType, 'http');
        if (!proxyOk) {
          throw new Error(`Failed to set proxy type for ${currentId}`);
        }

        const hasRequestedEgress =
          (newEntryEgressType === 'ovpn' && newEntryOvpn) ||
          (newEntryEgressType === 'upstream' && newEntryUpstreamProxyId);
        if (hasRequestedEgress) {
          const egressOk = await saveEgress(targetPort, newEntryEgressType, {
            ovpn: newEntryOvpn,
            upstreamProxyId: newEntryUpstreamProxyId,
          });
          if (!egressOk) throw new Error(`Assigned ID ${currentId} but failed to save egress.`);
        }

        const refreshMins = Math.max(0, Math.floor(Number(newEntryUpstreamRefreshMinutes) || 0));
        if (newEntryEgressType === 'upstream' && refreshMins > 0) {
          const refreshOk = await saveUpstreamRefresh(targetPort, refreshMins);
          if (!refreshOk) throw new Error(`Failed to save upstream refresh for ${currentId}`);
        }
      }

      setNewEntryId('');
      setNewEntryOvpn('');
      setNewEntryEgressType('ovpn');
      setNewEntryUpstreamProxyId('');
      setNewEntryProxyType('http');
      setNewEntryUpstreamRefreshMinutes('0');
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

  const openEditModal = (port, currentId, currentEgress, currentProxyType, currentRefresh) => {
    setEditTargetPort(port);
    setEditEntryId(currentId || '');
    const egressType = currentEgress?.type === 'upstream' ? 'upstream' : 'ovpn';
    setEditEntryEgressType(egressType);
    setEditEntryOvpn(currentEgress?.type === 'ovpn' ? currentEgress.ovpn || '' : '');
    setEditEntryUpstreamProxyId(currentEgress?.type === 'upstream' ? currentEgress.upstreamProxyId || '' : '');
    const pt = currentProxyType === 'socks5' ? 'socks5' : 'http';
    setEditEntryProxyType(pt);
    setEditProxyTypeInitial(pt);
    const refreshMins = Math.max(0, Math.floor(Number(currentRefresh) || 0));
    setEditEntryUpstreamRefreshMinutes(String(refreshMins));
    setEditUpstreamRefreshInitial(refreshMins);
    setShowEditEntry(true);
  };

  const handleEditEntrySubmit = async (e) => {
    e.preventDefault();
    if (!editTargetPort) return;
    setIsEditingEntry(true);
    setError('');

    try {
      const setLauncherRes = await fetch(`/api/set-launcher-id?port=${encodeURIComponent(editTargetPort)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ launcherId: editEntryId.trim() }),
      });
      
      if (!setLauncherRes.ok) {
        const errorData = await setLauncherRes.json().catch(() => ({}));
        throw new Error(errorData.error || 'Failed to update Launcher ID');
      }

      const egressOk = await saveEgress(editTargetPort, editEntryEgressType, {
        ovpn: editEntryOvpn,
        upstreamProxyId: editEntryUpstreamProxyId,
      });
      if (!egressOk) {
        throw new Error('Failed to update egress');
      }

      const proxyOk = await saveProxyType(editTargetPort, editEntryProxyType, editProxyTypeInitial);
      if (!proxyOk) {
        throw new Error('Failed to update proxy type');
      }

      const nextRefreshMins = Math.max(0, Math.floor(Number(editEntryUpstreamRefreshMinutes) || 0));
      if (editEntryEgressType === 'upstream' && nextRefreshMins !== editUpstreamRefreshInitial) {
        const refreshOk = await saveUpstreamRefresh(editTargetPort, nextRefreshMins);
        if (!refreshOk) throw new Error('Failed to update upstream refresh');
      }
      if (editEntryEgressType !== 'upstream' && editUpstreamRefreshInitial > 0) {
        await saveUpstreamRefresh(editTargetPort, 0);
      }

      setShowEditEntry(false);
      setEditTargetPort(null);
      
      const refreshed = await fetch('/api/status').then(r => r.json());
      setStatus(refreshed);
      setSelectedByPort(refreshed.assignedOvpnByPort || {});
    } catch (err) {
      setError('Failed to edit entry: ' + err.message);
    } finally {
      setIsEditingEntry(false);
    }
  };

  const runDeleteApis = async (port) => {
    await fetch(`/api/deactivate?port=${encodeURIComponent(port)}`, { method: 'POST' });
    await fetch(`/api/set-launcher-id?port=${encodeURIComponent(port)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ launcherId: '' }),
    });
    await fetch(`/api/set-egress?port=${encodeURIComponent(port)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'none' }),
    });
    await fetch(`/api/set-proxy-type?port=${encodeURIComponent(port)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proxyType: 'http' }),
    });
    await fetch(`/api/set-rotation?port=${encodeURIComponent(port)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ intervalMinutes: 0, country: '' }),
    });
    await fetch(`/api/set-upstream-refresh?port=${encodeURIComponent(port)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ intervalMinutes: 0 }),
    });
  };

  const deleteEntry = async (port) => {
    const accepted = await confirmAction({
      title: 'Delete this entry?',
      message: 'The launcher ID, egress, proxy type, rotation, and active state for this port will be cleared.',
      confirmLabel: 'Delete entry',
      variant: 'danger',
    });
    if (!accepted) return;
    setBusyPort(port);
    setError('');
    try {
      await runDeleteApis(port);
      const refreshed = await fetch('/api/status').then((r) => r.json());
      setStatus(refreshed);
      setSelectedByPort(refreshed.assignedOvpnByPort || {});
      setSelectedTablePorts((prev) => prev.filter((x) => x !== port));
      toast({ title: 'Entry deleted', message: `Port ${port} has been cleared.`, variant: 'success' });
    } catch (err) {
      setError('Failed to delete entry: ' + err.message);
      toast({ title: 'Delete failed', message: err.message, variant: 'danger' });
    } finally {
      setBusyPort(null);
    }
  };

  const refreshStatus = async () => {
    const refreshed = await fetch('/api/status').then((r) => r.json());
    setStatus(refreshed);
    setSelectedByPort(refreshed.assignedOvpnByPort || {});
  };

  const batchDeleteSelected = async (validPorts) => {
    const targets = selectedTablePorts.filter((p) => validPorts.includes(p));
    if (targets.length === 0) return;
    const accepted = await confirmAction({
      title: `Delete ${targets.length} selected ${targets.length === 1 ? 'entry' : 'entries'}?`,
      message: 'Selected ports will be stopped and cleared from the launcher list.',
      confirmLabel: 'Delete selected',
      variant: 'danger',
    });
    if (!accepted) return;
    setBatchBusy(true);
    setError('');
    try {
      for (const port of targets) {
        await runDeleteApis(port);
      }
      await refreshStatus();
      setSelectedTablePorts([]);
      toast({
        title: 'Entries deleted',
        message: `${targets.length} ${targets.length === 1 ? 'entry was' : 'entries were'} cleared.`,
        variant: 'success',
      });
    } catch (err) {
      setError('Batch delete failed: ' + err.message);
      toast({ title: 'Batch delete failed', message: err.message, variant: 'danger' });
    } finally {
      setBatchBusy(false);
    }
  };

  const batchApplyProxyType = async (validPorts, type) => {
    const targets = selectedTablePorts.filter((p) => validPorts.includes(p));
    if (targets.length === 0) return;
    const snap = status;
    if (!snap) return;
    const portBase = snap.portBase;
    const locs = snap.locations || [];
    setBatchBusy(true);
    setError('');
    try {
      for (const port of targets) {
        const idx = port - portBase;
        const loc = idx >= 0 && idx < locs.length ? locs[idx] : null;
        const prev = loc?.proxyType === 'socks5' ? 'socks5' : 'http';
        const ok = await saveProxyType(port, type, prev);
        if (!ok) {
          throw new Error('Proxy update rejected by server');
        }
      }
      await refreshStatus();
    } catch (err) {
      setError('Batch proxy update failed: ' + err.message);
    } finally {
      setBatchBusy(false);
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
  const egressByPort = status.egressByPort || {};
  const upstreamProxyById = Object.fromEntries(upstreamProxies.map((proxy) => [proxy.id, proxy]));

  const egressForPort = (portKey) => {
    const typed = egressByPort[portKey];
    if (typed?.type && typed.type !== 'none') return typed;
    const selectedOvpn = selectedByPort[portKey] || '';
    return selectedOvpn ? { type: 'ovpn', ovpn: selectedOvpn } : { type: 'none' };
  };

  const egressDisplay = (egress) => {
    if (egress?.type === 'upstream') {
      const profile = egress.upstreamProxy || upstreamProxyById[egress.upstreamProxyId];
      return profile?.label || profile?.host || egress.upstreamProxyId || 'Upstream proxy';
    }
    if (egress?.type === 'ovpn') {
      return formatOvpnDisplayLabel(egress.ovpn || '') || egress.ovpn || 'OVPN';
    }
    return '';
  };

  const proxyHost = status.clientProxyHost || '127.0.0.1';
  const proxyUser = status.proxyUsername ?? '';
  const proxyPass = status.proxyPassword ?? '';

  const encUrl = (s) => encodeURIComponent(s ?? '');
  const runningProxyRows = [];
  locations.forEach((loc, idx) => {
    const internalPort = internalPortForIndex(status, idx);
    if (internalPort == null) return;
    const portKey = String(internalPort);
    if (activationStateByPort[portKey] !== 'active') return;
    const hostPort = internalToPublishedPort(status, internalPort);
    const colonFormat = `${proxyHost}:${hostPort}:${proxyUser}:${proxyPass}`;
    const atFormat = `${proxyHost}:${hostPort}@${proxyUser}:${proxyPass}`;
    const ptype = loc.proxyType === 'socks5' ? 'socks5' : 'http';
    const schemeUrl =
      proxyUser || proxyPass
        ? `${ptype}://${encUrl(proxyUser)}:${encUrl(proxyPass)}@${proxyHost}:${hostPort}`
        : `${ptype}://${proxyHost}:${hostPort}`;
    const egress = egressForPort(portKey);
    const fileLabel = egressDisplay(egress);
    runningProxyRows.push({
      internalPort,
      hostPort,
      colonFormat,
      atFormat,
      schemeUrl,
      proxyType: ptype,
      label: fileLabel || loc.label || `Port #${idx}`,
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
    const hasEgress = egressForPort(portKey).type !== 'none';
    const isEnabled = enabledPorts.has(port);
    return hasLauncherId || hasEgress || isEnabled;
  });

  const configuredInternalPorts = configuredPortRows
    .map(({ idx }) => internalPortForIndex(status, idx))
    .filter((p) => p != null);
  const effectiveSelectedPorts = selectedTablePorts.filter((p) => configuredInternalPorts.includes(p));

  const launcherIdQuery = launcherIdFilter.trim().toLowerCase();
  const filteredPortRows = launcherIdQuery
    ? configuredPortRows.filter(({ loc }) => {
        const id = typeof loc.launcherId === 'string' ? loc.launcherId : '';
        return id.toLowerCase().includes(launcherIdQuery);
      })
    : configuredPortRows;

  const launcherTableSortedRows = (() => {
    const sortKey = launcherTableSort.key;
    if (!sortKey) return filteredPortRows;
    const mul = launcherTableSort.dir === 'desc' ? -1 : 1;
    return [...filteredPortRows].sort((a, b) => {
      const portA = internalPortForIndex(status, a.idx);
      const portB = internalPortForIndex(status, b.idx);
      if (portA == null && portB == null) return 0;
      if (portA == null) return 1;
      if (portB == null) return -1;

      const keyA = String(portA);
      const keyB = String(portB);

      if (sortKey === 'id') {
        const ida = typeof a.loc.launcherId === 'string' ? a.loc.launcherId.trim() : '';
        const idb = typeof b.loc.launcherId === 'string' ? b.loc.launcherId.trim() : '';
        const rankA = ida ? 0 : 1;
        const rankB = idb ? 0 : 1;
        if (rankA !== rankB) return (rankA - rankB) * mul;
        const cmp = ida.localeCompare(idb, undefined, { sensitivity: 'base', numeric: true });
        if (cmp !== 0) return cmp * mul;
        return (portA - portB) * mul;
      }

      if (sortKey === 'ovpn') {
        const oa = egressDisplay(egressForPort(keyA));
        const ob = egressDisplay(egressForPort(keyB));
        const rankA = oa ? 0 : 1;
        const rankB = ob ? 0 : 1;
        if (rankA !== rankB) return (rankA - rankB) * mul;
        const cmp = oa.localeCompare(ob, undefined, { sensitivity: 'base', numeric: true });
        if (cmp !== 0) return cmp * mul;
        return (portA - portB) * mul;
      }

      return 0;
    });
  })();

  const visibleInternalPorts = launcherTableSortedRows
    .map(({ idx }) => internalPortForIndex(status, idx))
    .filter((p) => p != null);
  const selectedInViewCount = visibleInternalPorts.filter((p) => selectedTablePorts.includes(p)).length;
  const allVisibleSelected =
    visibleInternalPorts.length > 0 && selectedInViewCount === visibleInternalPorts.length;

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
      toast({ title: 'Copied', message: 'Value copied to clipboard.', variant: 'success', duration: 1800 });
    } catch (err) {
      setError(err?.message ? `Copy failed: ${err.message}` : 'Copy failed');
      toast({ title: 'Copy failed', message: err?.message || 'Could not copy value.', variant: 'danger' });
    }
  };

  const authRouting = status.authRouting?.enabled ? status.authRouting : null;
  const providerOptions = Array.from(new Set(ovpnRouteRows.map((row) => row.provider))).sort((a, b) =>
    a.localeCompare(b, undefined, { sensitivity: 'base', numeric: true })
  );
  const selectedCreateProvider = createProxyProvider || providerOptions[0] || '';
  const providerRouteRows = ovpnRouteRows.filter((row) => row.provider === selectedCreateProvider);
  const locationOptions = Array.from(new Set(providerRouteRows.map((row) => row.location))).sort((a, b) =>
    a.localeCompare(b, undefined, { sensitivity: 'base', numeric: true })
  );
  const selectedCreateLocation = createProxyLocation || locationOptions[0] || '';
  const locationRouteRows = providerRouteRows.filter((row) => row.location === selectedCreateLocation);
  const selectedCreateOvpn =
    createProxyOvpn ||
    locationRouteRows[0]?.file ||
    providerRouteRows[0]?.file ||
    sortedOvpnFiles[0] ||
    '';
  const selectedCreateRouteRow =
    ovpnRouteRows.find((row) => row.file === selectedCreateOvpn) || {
      file: selectedCreateOvpn,
      provider: selectedCreateProvider,
      location: selectedCreateLocation,
    };
  const selectedCreateUpstreamProxy = upstreamProxies.find((proxy) => proxy.id === createProxyUpstreamProxyId) || null;
  const createProxyPreviewSource =
    createProxyEgressType === 'upstream'
      ? createProxyExternalId ||
        createProxyLabel ||
        selectedCreateUpstreamProxy?.label ||
        selectedCreateUpstreamProxy?.host ||
        createProxyUpstreamProxyId ||
        'upstream_proxy'
      : createProxyExternalId ||
        createProxyLabel ||
        [selectedCreateRouteRow.provider, selectedCreateRouteRow.location].filter(Boolean).join('_');
  const createProxyUsernamePreview = `${slugifyProxyUsername(
    createProxyPreviewSource
  )}_${createProxySuffix}`;
  const canCreateProxy =
    createProxyEgressType === 'upstream' ? Boolean(createProxyUpstreamProxyId) : Boolean(selectedCreateOvpn);

  const openCreateProxyModal = () => {
    const provider = providerOptions[0] || '';
    const rows = ovpnRouteRows.filter((row) => row.provider === provider);
    const location = rows[0]?.location || '';
    const ovpn = rows[0]?.file || '';
    setCreateProxyEgressType('ovpn');
    setCreateProxyProvider(provider);
    setCreateProxyLocation(location);
    setCreateProxyOvpn(ovpn);
    setCreateProxyUpstreamProxyId('');
    setCreateProxyLabel('');
    setCreateProxyExternalId('');
    setCreateProxyType('http');
    setCreateProxyRotationMinutes('0');
    setCreateProxyRotationCountry('');
    setCreateProxySuffix(randomProxySuffix());
    setCreatedProxy(null);
    setShowCreateProxy(true);
  };

  const handleCreateProxyProviderChange = (provider) => {
    const rows = ovpnRouteRows.filter((row) => row.provider === provider);
    setCreateProxyProvider(provider);
    setCreateProxyLocation(rows[0]?.location || '');
    setCreateProxyOvpn(rows[0]?.file || '');
    setCreatedProxy(null);
  };

  const handleCreateProxyLocationChange = (location) => {
    const rows = providerRouteRows.filter((row) => row.location === location);
    setCreateProxyLocation(location);
    setCreateProxyOvpn(rows[0]?.file || '');
    setCreatedProxy(null);
  };

  const handleCreateProxyRoute = async (event) => {
    event.preventDefault();
    const ovpn = selectedCreateOvpn;
    const upstreamProxyId = createProxyUpstreamProxyId;
    const isUpstreamCreate = createProxyEgressType === 'upstream';
    if (!isUpstreamCreate && !ovpn) {
      setError('Select an OVPN profile first.');
      return;
    }
    if (isUpstreamCreate && !upstreamProxyId) {
      setError('Select an upstream proxy first.');
      return;
    }
    const fallbackLabel = isUpstreamCreate
      ? selectedCreateUpstreamProxy?.label || selectedCreateUpstreamProxy?.host || upstreamProxyId
      : formatOvpnDisplayLabel(ovpn);
    const payload = {
      autoGenerateUsername: true,
      username: createProxyUsernamePreview,
      label: createProxyLabel.trim() || createProxyExternalId.trim() || fallbackLabel,
      externalId: createProxyExternalId.trim(),
      proxyType: createProxyType === 'socks5' ? 'socks5' : 'http',
      enabled: true,
      rotationIntervalMinutes: isUpstreamCreate
        ? 0
        : Math.max(0, Math.floor(Number(createProxyRotationMinutes) || 0)),
      rotationCountry: isUpstreamCreate ? '' : (createProxyRotationCountry || '').toUpperCase(),
      egress: isUpstreamCreate ? { type: 'upstream', upstreamProxyId } : { type: 'ovpn', ovpn },
    };
    setCreatingProxy(true);
    setAuthRouteBusy(`create:${createProxyUsernamePreview}`);
    setError('');
    try {
      const res = await fetch('/api/auth-route', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!data.ok) {
        setError(data.error || 'Failed to create proxy');
        return;
      }
      const route = data.route || {};
      await refreshStatus();
      setCreatedProxy({
        username: route.username || createProxyUsernamePreview,
        label: route.label || payload.label,
        proxyType: route.proxyType || payload.proxyType,
        egress: payload.egress,
      });
      setCreateProxySuffix(randomProxySuffix());
      toast({ title: 'Proxy created', message: route.username || createProxyUsernamePreview, variant: 'success' });
    } catch (err) {
      setError('Failed to create proxy: ' + err.message);
    } finally {
      setCreatingProxy(false);
      setAuthRouteBusy(null);
    }
  };

  const resetAuthRouteForm = () => {
    setAuthRouteForm({
      username: '',
      label: '',
      externalId: '',
      proxyType: 'http',
      enabled: true,
      egressType: 'ovpn',
      ovpn: '',
      upstreamProxyId: '',
      rotationMinutes: '0',
      rotationCountry: '',
    });
  };

  const loadAuthRouteForEdit = (route) => {
    const egress = route.egress || {};
    setAuthRouteForm({
      username: route.username || '',
      label: route.label || route.username || '',
      externalId: route.externalId || '',
      proxyType: route.proxyType === 'socks5' ? 'socks5' : 'http',
      enabled: route.enabled !== false,
      egressType: egress.type === 'upstream' ? 'upstream' : 'ovpn',
      ovpn: egress.type === 'ovpn' ? egress.ovpn || '' : '',
      upstreamProxyId: egress.type === 'upstream' ? egress.upstreamProxyId || '' : '',
      rotationMinutes: String(Math.max(0, Math.floor(Number(route.rotationIntervalMinutes) || 0))),
      rotationCountry: (route.rotationCountry || '').toUpperCase(),
    });
    setShowAuthRouteEditor(true);
  };

  const saveAuthRoute = async (event) => {
    event.preventDefault();
    const username = authRouteForm.username.trim();
    if (!username) {
      setError('Username is required.');
      return;
    }
    const payload = {
      username,
      label: authRouteForm.label.trim() || username,
      externalId: authRouteForm.externalId.trim(),
      proxyType: authRouteForm.proxyType === 'socks5' ? 'socks5' : 'http',
      enabled: authRouteForm.enabled,
      rotationIntervalMinutes:
        authRouteForm.egressType === 'ovpn'
          ? Math.max(0, Math.floor(Number(authRouteForm.rotationMinutes) || 0))
          : 0,
      rotationCountry:
        authRouteForm.egressType === 'ovpn' ? (authRouteForm.rotationCountry || '').toUpperCase() : '',
      egress:
        authRouteForm.egressType === 'upstream'
          ? { type: 'upstream', upstreamProxyId: authRouteForm.upstreamProxyId }
          : { type: 'ovpn', ovpn: authRouteForm.ovpn },
    };
    setAuthRouteBusy(`save:${username}`);
    setError('');
    try {
      const res = await fetch('/api/auth-route', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!data.ok) {
        setError(data.error || 'Failed to save route');
        return;
      }
      await refreshStatus();
      resetAuthRouteForm();
      setShowAuthRouteEditor(false);
      toast({ title: 'Route saved', message: username, variant: 'success' });
    } catch (err) {
      setError('Failed to save route: ' + err.message);
    } finally {
      setAuthRouteBusy(null);
    }
  };

  const exportAuthRoutes = async () => {
    const targets = authRoutesList.filter((route) => selectedAuthRoutes.includes(route.username));
    if (targets.length === 0) {
      toast({
        title: 'Nothing selected',
        message: 'Select at least one route to export.',
        variant: 'warning',
      });
      return;
    }
    setError('');
    try {
      const res = await fetch('/api/export-auth-routes');
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to export auth routes');
      const selectedUsernames = new Set(targets.map((route) => route.username));
      const exportPayload = {
        ...data,
        routes: (Array.isArray(data.routes) ? data.routes : []).filter((route) =>
          selectedUsernames.has(route.username),
        ),
      };
      const blob = new Blob([JSON.stringify(exportPayload, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = 'portico-auth-routes.json';
      anchor.click();
      URL.revokeObjectURL(url);
      const count = exportPayload.routes.length;
      toast({
        title: 'Export complete',
        message: `${count} auth ${count === 1 ? 'route' : 'routes'} exported.`,
        variant: 'success',
      });
    } catch (err) {
      setError(err.message || 'Failed to export auth routes');
      toast({ title: 'Export failed', message: err.message || 'Failed to export auth routes.', variant: 'danger' });
    }
  };

  const importAuthRoutesFromFile = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    setError('');
    let payload;
    try {
      payload = JSON.parse(await file.text());
    } catch {
      toast({ title: 'Invalid JSON file', message: 'The imported file could not be parsed.', variant: 'danger' });
      return;
    }
    const routes = Array.isArray(payload?.routes)
      ? payload.routes
      : Array.isArray(payload?.authRouting?.routes)
        ? payload.authRouting.routes
        : null;
    if (!Array.isArray(routes)) {
      toast({ title: 'Invalid file', message: 'Expected a routes array in the export file.', variant: 'danger' });
      return;
    }
    const accepted = await confirmAction({
      title: 'Import auth routes',
      message: `Replace all auth routes with ${routes.length} route(s) from "${file.name}"? Running route workers will be stopped.`,
      confirmLabel: 'Import',
      variant: 'danger',
    });
    if (!accepted) return;
    setAuthRoutesImportBusy(true);
    try {
      const res = await fetch('/api/import-auth-routes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'replace', routes }),
      });
      const data = await res.json();
      if (!res.ok) {
        const failed = Array.isArray(data.results)
          ? data.results.filter((row) => !row.ok).map((row) => `${row.username}: ${row.error}`).join('; ')
          : '';
        throw new Error(data.error || failed || 'Failed to import auth routes');
      }
      await refreshStatus();
      toast({
        title: 'Import complete',
        message: `${data.imported || routes.length} auth route(s) imported.`,
        variant: 'success',
      });
    } catch (err) {
      setError(err.message || 'Failed to import auth routes');
      toast({ title: 'Import failed', message: err.message || 'Failed to import auth routes.', variant: 'danger' });
    } finally {
      setAuthRoutesImportBusy(false);
    }
  };

  const runAuthRouteAction = async (username, action) => {
    const endpoint =
      action === 'delete'
        ? '/api/auth-route-delete'
        : action === 'restart'
          ? '/api/auth-route-restart'
          : action === 'stop'
            ? '/api/auth-route-stop'
            : '/api/auth-route-start';
    if (action === 'delete') {
      const ok = await confirmAction({
        title: 'Delete Route',
        message: `Delete ${username}? Running workers for this route will be stopped.`,
        confirmText: 'Delete',
        variant: 'danger',
      });
      if (!ok) return;
    }
    setAuthRouteBusy(`${action}:${username}`);
    setError('');
    try {
      const res = await fetch(`${endpoint}?username=${encodeURIComponent(username)}`, { method: 'POST' });
      const data = await res.json();
      if (!data.ok) {
        const detail = Array.isArray(data.results)
          ? data.results.filter((r) => !r.ok).map((r) => `${r.scheme}: ${r.error}`).join('; ')
          : '';
        setError(data.error || detail || `Failed to ${action} route`);
        return;
      }
      if (action === 'delete') {
        setSelectedAuthRoutes((current) => current.filter((item) => item !== username));
      }
      await refreshStatus();
      toast({ title: 'Route updated', message: `${action}: ${username}`, variant: 'success' });
    } catch (err) {
      setError(`Failed to ${action} route: ` + err.message);
    } finally {
      setAuthRouteBusy(null);
    }
  };

  const toggleAuthRouteSelection = (username) => {
    setSelectedAuthRoutes((current) =>
      current.includes(username) ? current.filter((item) => item !== username) : [...current, username],
    );
  };

  const toggleVisibleAuthRoutes = () => {
    setSelectedAuthRoutes((current) => {
      if (allVisibleAuthRoutesSelected) {
        return current.filter((username) => !visibleAuthRouteUsernames.includes(username));
      }
      return Array.from(new Set([...current, ...visibleAuthRouteUsernames]));
    });
  };

  const saveSdFarmProxyType = async (nextType) => {
    const normalized = nextType === 'socks5' ? 'socks5' : 'http';
    setSdFarmProxyTypeSaving(true);
    setError('');
    try {
      const current = await fetch('/api/sd-farm/settings').then((res) => res.json());
      const res = await fetch('/api/sd-farm/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sdFarmRoot: current.sdFarmRoot || '',
          sdFarmSource: current.sdFarmSource || 'import',
          ixBrowserApiBase: current.ixBrowserApiBase || '',
          ixBrowserProxyHost: current.ixBrowserProxyHost || '',
          ixBrowserProxyType: normalized,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to save SD Farm proxy type');
      setSdFarmProxyType(normalized);
      toast({
        title: 'SD Farm proxy type saved',
        message: `New syncs use ${normalized === 'socks5' ? 'SOCKS5' : 'HTTP'} auth-routing port.`,
        variant: 'success',
      });
    } catch (err) {
      setError(err.message || 'Failed to save SD Farm proxy type');
      toast({
        title: 'Save failed',
        message: err.message || 'Failed to save SD Farm proxy type',
        variant: 'danger',
      });
    } finally {
      setSdFarmProxyTypeSaving(false);
    }
  };

  const batchDeleteAuthRoutes = async () => {
    const targets = selectedAuthRoutes.filter((username) =>
      authRoutesList.some((route) => route.username === username),
    );
    if (targets.length === 0) return;
    const accepted = await confirmAction({
      title: `Delete ${targets.length} selected ${targets.length === 1 ? 'route' : 'routes'}?`,
      message: 'Running workers for selected routes will be stopped.',
      confirmLabel: 'Delete selected',
      variant: 'danger',
    });
    if (!accepted) return;
    setAuthRoutesBatchBusy(true);
    setError('');
    try {
      for (const username of targets) {
        const res = await fetch(`/api/auth-route-delete?username=${encodeURIComponent(username)}`, {
          method: 'POST',
        });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || `Failed to delete ${username}`);
      }
      setSelectedAuthRoutes([]);
      await refreshStatus();
      toast({
        title: 'Routes deleted',
        message: `${targets.length} auth ${targets.length === 1 ? 'route was' : 'routes were'} removed.`,
        variant: 'success',
      });
    } catch (err) {
      setError('Batch delete failed: ' + err.message);
      toast({ title: 'Batch delete failed', message: err.message, variant: 'danger' });
    } finally {
      setAuthRoutesBatchBusy(false);
    }
  };

  const batchApplyAuthRouteProxyType = async (type) => {
    const normalized = type === 'socks5' ? 'socks5' : 'http';
    const targets = authRoutesList.filter((route) => selectedAuthRoutes.includes(route.username));
    if (targets.length === 0) return;
    setAuthRoutesBatchBusy(true);
    setError('');
    try {
      for (const route of targets) {
        const res = await fetch('/api/auth-route', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(authRoutePayloadFromRoute(route, { proxyType: normalized })),
        });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || `Failed to update ${route.username}`);
      }
      await refreshStatus();
      toast({
        title: 'Proxy type updated',
        message: `${targets.length} ${targets.length === 1 ? 'route' : 'routes'} set to ${normalized.toUpperCase()}.`,
        variant: 'success',
      });
    } catch (err) {
      setError('Batch proxy update failed: ' + err.message);
      toast({ title: 'Batch update failed', message: err.message, variant: 'danger' });
    } finally {
      setAuthRoutesBatchBusy(false);
    }
  };

  const editSelectedAuthRoute = () => {
    const targets = authRoutesList.filter((route) => selectedAuthRoutes.includes(route.username));
    if (targets.length !== 1) {
      toast({
        title: 'Select one route',
        message: 'Choose exactly one route to edit, or use Set HTTP/SOCKS5 for bulk proxy type changes.',
        variant: 'warning',
      });
      return;
    }
    loadAuthRouteForEdit(targets[0]);
  };

  if (authRouting) {
    const browserHost =
      typeof window !== 'undefined' && window.location?.hostname
        ? window.location.hostname
        : '';
    const normalizedBrowserHost =
      browserHost === 'localhost' || browserHost === '[::1]' || browserHost === '::1'
        ? '127.0.0.1'
        : browserHost || '127.0.0.1';
    const authCopyHost =
      authRouting.copyHost ||
      (authRouting.copyHostMode === 'local'
        ? normalizedBrowserHost
        : authRouting.clientProxyHost || status.clientProxyHost || '127.0.0.1');
    const authHostSource =
      authRouting.copyHostSource ||
      (authRouting.copyHostMode === 'local'
        ? 'browser-local'
        : authRouting.clientProxyHostSource || status.clientProxyHostSource || '');
    const authPassword = authRouting.globalPassword || '';
    const httpPort = authRouting.httpPort || 58080;
    const socksPort = authRouting.socksPort || 58081;
    const routes = authRoutesList;
    const localAuthMode = authRouting.localAuthRouting
      ? 'local'
      : authRouting.copyHostMode || 'server';
    const localAuthLabel =
      localAuthMode === 'local'
        ? 'Local auth'
        : localAuthMode === 'configured'
          ? 'Configured host'
          : 'Server WAN';
    const routeColonFormat = (username, port) => `${authCopyHost}:${port}:${username}:${authPassword}`;
    const routeAtFormat = (username, port) => `${username}:${authPassword}@${authCopyHost}:${port}`;

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
              <span className="material-symbols-outlined text-primary">route</span>
              <h3 className="font-bold">Auth routes</h3>
            </div>
            <div className="dashboard-row-actions">
              <span className="badge-primary">{routes.length} ROUTES</span>
              <button
                type="button"
                className="btn-outline"
                onClick={exportAuthRoutes}
                disabled={authRoutesImportBusy || selectedAuthRoutes.length === 0}
              >
                <span className="material-symbols-outlined">file_download</span>
                Export selected
              </button>
              <button
                type="button"
                className="btn-outline"
                onClick={() => authRoutesImportRef.current?.click()}
                disabled={authRoutesImportBusy}
              >
                <span className="material-symbols-outlined">file_upload</span>
                {authRoutesImportBusy ? 'Importing...' : 'Import'}
              </button>
              <input
                type="file"
                ref={authRoutesImportRef}
                onChange={importAuthRoutesFromFile}
                accept=".json,application/json"
                style={{ display: 'none' }}
              />
              <button type="button" className="btn-primary" onClick={openCreateProxyModal}>
                <span className="material-symbols-outlined">add</span>
                Create Proxy
              </button>
            </div>
          </div>
          <p className="text-muted text-sm px-4 pt-2 pb-0 mb-0">
            HTTP listens on <code className="text-mono">{httpPort}</code> and SOCKS5 listens on{' '}
            <code className="text-mono">{socksPort}</code>. Username selects the route.
          </p>
          <p className="text-muted text-sm px-4 pt-1 pb-0 mb-0">
            Connect host shown in proxy lines: <code className="text-mono">{authCopyHost}</code>
            {authHostSource && <span> ({authHostSource})</span>}
          </p>
          <div className="dashboard-auth-toolbar px-4 py-3 border-t border-[var(--border-color)]">
            <div className="dashboard-auth-mode-row">
              <span className={`dashboard-auth-mode-badge ${localAuthMode === 'local' ? 'mode-local' : ''}`}>
                <span className="material-symbols-outlined" aria-hidden>
                  {localAuthMode === 'local' ? 'home' : 'public'}
                </span>
                {localAuthLabel}
              </span>
              <label className="dashboard-auth-mode-control">
                <span>SD Farm proxy type</span>
                <select
                  className="input dashboard-auth-mode-select"
                  value={sdFarmProxyType}
                  disabled={sdFarmProxyTypeSaving || authRoutesBatchBusy}
                  onChange={(event) => saveSdFarmProxyType(event.target.value)}
                  aria-label="SD Farm proxy type for sync"
                >
                  <option value="http">HTTP ({httpPort})</option>
                  <option value="socks5">SOCKS5 ({socksPort})</option>
                </select>
              </label>
            </div>
            <div className="dashboard-auth-filter-row">
              <label className="dashboard-ports-launcher-search dashboard-auth-search">
                <span className="material-symbols-outlined" aria-hidden>
                  search
                </span>
                <input
                  type="search"
                  className="dashboard-ports-launcher-search-input"
                  value={authRouteSearch}
                  onChange={(event) => setAuthRouteSearch(event.target.value)}
                  placeholder="Search route, label, ID…"
                  aria-label="Filter auth routes"
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>
              <div className="dashboard-auth-filters" role="group" aria-label="Auth route filters">
                {[
                  { id: 'all', label: 'All' },
                  { id: 'sd_farm', label: 'SD Farm' },
                  { id: 'http', label: 'HTTP' },
                  { id: 'socks5', label: 'SOCKS5' },
                ].map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={`dashboard-auth-filter ${authRouteTypeFilter === item.id ? 'active' : ''}`}
                    onClick={() => setAuthRouteTypeFilter(item.id)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {selectedAuthRoutes.length > 0 && (
            <div className="dashboard-batch-bar flex flex-wrap items-center gap-2 px-4 py-3 border-t border-[var(--border-color)]">
              <span className="text-sm font-medium">
                {selectedAuthRoutes.length} selected
                {selectedAuthRoutesInView.length !== selectedAuthRoutes.length && (
                  <span className="text-muted font-normal">
                    {' '}
                    ({selectedAuthRoutesInView.length} visible)
                  </span>
                )}
              </span>
              <button
                type="button"
                className="btn-secondary text-sm"
                disabled={authRoutesBatchBusy || selectedAuthRoutes.length === 0}
                onClick={() => batchApplyAuthRouteProxyType('http')}
              >
                Set HTTP
              </button>
              <button
                type="button"
                className="btn-secondary text-sm"
                disabled={authRoutesBatchBusy || selectedAuthRoutes.length === 0}
                onClick={() => batchApplyAuthRouteProxyType('socks5')}
              >
                Set SOCKS5
              </button>
              <button
                type="button"
                className="btn-secondary text-sm"
                disabled={authRoutesBatchBusy || selectedAuthRoutes.length !== 1}
                onClick={editSelectedAuthRoute}
              >
                Edit selected
              </button>
              <button
                type="button"
                className="btn-danger text-sm"
                disabled={authRoutesBatchBusy || selectedAuthRoutes.length === 0}
                onClick={batchDeleteAuthRoutes}
              >
                Delete selected
              </button>
              <button
                type="button"
                className="btn-secondary text-sm"
                disabled={authRoutesBatchBusy}
                onClick={() => setSelectedAuthRoutes([])}
              >
                Clear selection
              </button>
            </div>
          )}

          <div className="table-container">
            <table className="data-table dashboard-copy-table">
              <thead>
                <tr>
                  <th className="dashboard-auth-check">
                    <input
                      type="checkbox"
                      checked={allVisibleAuthRoutesSelected}
                      disabled={visibleAuthRouteUsernames.length === 0 || authRoutesBatchBusy}
                      onChange={toggleVisibleAuthRoutes}
                      aria-label="Select visible auth routes"
                    />
                  </th>
                  <th>Route</th>
                  <th>Egress</th>
                  <th>Type</th>
                  <th>Status</th>
                  <th>VPN Public IP</th>
                  <th>IP:PORT:USER:PASS</th>
                  <th>USER:PASS@IP:PORT</th>
                  <th className="text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {routes.length === 0 ? (
                  <tr>
                    <td colSpan="9" className="text-center p-6 text-muted">No auth routes configured.</td>
                  </tr>
                ) : filteredAuthRoutes.length === 0 ? (
                  <tr>
                    <td colSpan="9" className="text-center p-6 text-muted">No auth routes match this filter.</td>
                  </tr>
                ) : (
                  filteredAuthRoutes.map((route) => {
                    const routeType = route.proxyType === 'socks5' ? 'socks5' : 'http';
                    const routeProtocol = route.protocols?.[routeType] || {};
                    const routeState = routeProtocol.status || 'inactive';
                    const egressPublicIp = routeProtocol.egressPublicIp || '';
                    const egressPublicIpError = routeProtocol.egressPublicIpError || '';
                    const routePort = routeType === 'socks5' ? socksPort : httpPort;
                    const routeColon = routeColonFormat(route.username, routePort);
                    const routeAt = routeAtFormat(route.username, routePort);
                    const busy = authRouteBusy && authRouteBusy.endsWith(`:${route.username}`);
                    const rowChecked = selectedAuthRoutes.includes(route.username);
                    const rotationMinutes = Math.max(0, Math.floor(Number(route.rotationIntervalMinutes) || 0));
                    const rotationCountry = (route.rotationCountry || '').toUpperCase();
                    const isRotating = route.egress?.type === 'ovpn' && rotationMinutes > 0;
                    return (
                      <tr key={route.username} className={route.enabled ? undefined : 'opacity-60'}>
                        <td className="dashboard-auth-check" data-label="Select">
                          <input
                            type="checkbox"
                            checked={rowChecked}
                            disabled={authRoutesBatchBusy || busy}
                            onChange={() => toggleAuthRouteSelection(route.username)}
                            aria-label={`Select ${route.username}`}
                          />
                        </td>
                        <td data-label="Route">
                          <div className="flex flex-col gap-1">
                            <span className="font-bold">{route.label || route.username}</span>
                            <button
                              type="button"
                              className="dashboard-copy-line text-mono"
                              onClick={() => copyProxyLine(route.username, `auth-user-${route.username}`)}
                            >
                              <span className="dashboard-copy-code">{route.username}</span>
                              {copiedToken === `auth-user-${route.username}` && <span className="dashboard-copy-toast">Copied</span>}
                            </button>
                            {!route.enabled && <span className="status-inactive">Disabled</span>}
                            {route.externalId && <span className="text-muted text-sm">ID: {route.externalId}</span>}
                          </div>
                        </td>
                        <td data-label="Egress">
                          <div className="dashboard-ovpn-cell">
                            <span className="dashboard-egress-kind">
                              {route.egress?.type === 'upstream' ? 'Upstream Proxy' : route.egress?.type === 'ovpn' ? 'OpenVPN' : 'No egress'}
                            </span>
                            <span className="text-mono text-sm">{egressDisplay(route.egress) || 'Not configured'}</span>
                          </div>
                        </td>
                        <td data-label="Type">
                          <span className="badge-primary">{routeType === 'socks5' ? 'SOCKS5' : 'HTTP'}</span>
                        </td>
                        <td data-label="Status">
                          <div className="flex flex-col gap-1">
                            <span className={routeState === 'active' ? 'status-active' : routeState === 'starting' ? 'status-starting' : routeState === 'failed' ? 'status-failed' : 'status-inactive'}>
                              {routeType === 'socks5' ? 'SOCKS5' : 'HTTP'}: {routeState}
                            </span>
                            {isRotating && (
                              <span
                                className="dashboard-rotation-badge"
                                title={`Rotates every ${rotationMinutes}m${
                                  rotationCountry ? ` from ${rotationCountry} pool` : ''
                                }`}
                              >
                                <span className="material-symbols-outlined" aria-hidden>
                                  autorenew
                                </span>
                                <span>
                                  Rotating - {rotationMinutes}m
                                  {rotationCountry ? ` - ${rotationCountry}` : ''}
                                </span>
                              </span>
                            )}
                          </div>
                        </td>
                        <td data-label="VPN Public IP">
                          <div className="flex flex-col gap-1">
                            <span className="text-mono text-sm">
                              {routeState === 'active'
                                ? egressPublicIp || (egressPublicIpError ? 'Unavailable' : 'Checking...')
                                : 'Inactive'}
                            </span>
                            {routeState === 'active' && egressPublicIpError && (
                              <span className="status-error-text">{egressPublicIpError}</span>
                            )}
                          </div>
                        </td>
                        <td data-label="IP:PORT:USER:PASS">
                          <button type="button" className="dashboard-copy-line" onClick={() => copyProxyLine(routeColon, `auth-colon-${route.username}`)}>
                            <code className="dashboard-copy-code">{routeColon}</code>
                            {copiedToken === `auth-colon-${route.username}` && <span className="dashboard-copy-toast">Copied</span>}
                          </button>
                        </td>
                        <td data-label="USER:PASS@IP:PORT">
                          <button type="button" className="dashboard-copy-line" onClick={() => copyProxyLine(routeAt, `auth-at-${route.username}`)}>
                            <code className="dashboard-copy-code">{routeAt}</code>
                            {copiedToken === `auth-at-${route.username}` && <span className="dashboard-copy-toast">Copied</span>}
                          </button>
                        </td>
                        <td className="text-right" data-label="Actions">
                          <div className="dashboard-auth-actions">
                            {routeState === 'active' || routeState === 'starting' ? (
                              <button
                                type="button"
                                className="dashboard-auth-action dashboard-auth-action-stop"
                                disabled={busy || routeState === 'starting'}
                                onClick={() => runAuthRouteAction(route.username, 'stop')}
                                title={routeState === 'starting' ? 'Starting' : 'Stop route'}
                                aria-label={routeState === 'starting' ? 'Route starting' : 'Stop route'}
                              >
                                <span className="material-symbols-outlined">stop_circle</span>
                              </button>
                            ) : (
                              <button
                                type="button"
                                className="dashboard-auth-action dashboard-auth-action-start"
                                disabled={busy || !route.enabled}
                                onClick={() => runAuthRouteAction(route.username, 'start')}
                                title="Start route"
                                aria-label="Start route"
                              >
                                <span className="material-symbols-outlined">play_circle</span>
                              </button>
                            )}
                            <button
                              type="button"
                              className="dashboard-auth-action dashboard-auth-action-restart"
                              disabled={busy || routeState !== 'active'}
                              onClick={() => runAuthRouteAction(route.username, 'restart')}
                              title={routeState === 'active' ? 'Restart route' : 'Restart available when active'}
                              aria-label="Restart route"
                            >
                              <span className="material-symbols-outlined">restart_alt</span>
                            </button>
                            <button
                              type="button"
                              className="dashboard-auth-action"
                              disabled={busy}
                              onClick={() => loadAuthRouteForEdit(route)}
                              title="Edit route"
                              aria-label="Edit route"
                            >
                              <span className="material-symbols-outlined">edit</span>
                            </button>
                            <button
                              type="button"
                              className="dashboard-auth-action dashboard-auth-action-delete"
                              disabled={busy}
                              onClick={() => runAuthRouteAction(route.username, 'delete')}
                              title="Delete route"
                              aria-label="Delete route"
                            >
                              <span className="material-symbols-outlined">delete</span>
                            </button>
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

        {showCreateProxy && (
          <div className="dashboard-modal-overlay" onClick={() => setShowCreateProxy(false)}>
            <div
              className="dashboard-modal-panel dashboard-create-proxy-panel"
              onClick={(e) => e.stopPropagation()}
              role="dialog"
              aria-modal="true"
              aria-labelledby="dashboard-create-proxy-title"
            >
              <div className="dashboard-modal-header">
                <h3 id="dashboard-create-proxy-title" className="dashboard-modal-title">
                  Create Proxy
                </h3>
                <button
                  type="button"
                  onClick={() => setShowCreateProxy(false)}
                  className="dashboard-modal-close"
                  aria-label="Close"
                >
                  <span className="material-symbols-outlined">close</span>
                </button>
              </div>
              <form className="dashboard-modal-body" onSubmit={handleCreateProxyRoute}>
                <div className="form-grid">
                  <label className="form-field">
                    <span>Egress</span>
                    <select
                      className="input"
                      value={createProxyEgressType}
                      onChange={(e) => {
                        const nextType = e.target.value === 'upstream' ? 'upstream' : 'ovpn';
                        setCreateProxyEgressType(nextType);
                        setCreatedProxy(null);
                        if (nextType === 'upstream') {
                          setCreateProxyRotationMinutes('0');
                          setCreateProxyRotationCountry('');
                        }
                      }}
                      disabled={creatingProxy}
                    >
                      <option value="ovpn">OpenVPN</option>
                      <option value="upstream">Upstream proxy</option>
                    </select>
                  </label>
                  {createProxyEgressType === 'upstream' && (
                    <label className="form-field">
                      <span>Saved upstream proxy</span>
                      <select
                        className="input"
                        value={createProxyUpstreamProxyId}
                        onChange={(e) => {
                          setCreateProxyUpstreamProxyId(e.target.value);
                          setCreatedProxy(null);
                        }}
                        disabled={creatingProxy}
                        required
                      >
                        <option value="">Select upstream proxy...</option>
                        {upstreamProxies.map((proxy) => (
                          <option key={proxy.id} value={proxy.id}>
                            {proxy.label || proxy.host || proxy.id} ({proxy.scheme}://{proxy.host}:{proxy.port})
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                </div>
                {createProxyEgressType === 'ovpn' && (
                  <>
                    <div className="form-grid">
                      <label className="form-field">
                        <span>Provider</span>
                        <select
                          className="input"
                          value={selectedCreateProvider}
                          onChange={(e) => handleCreateProxyProviderChange(e.target.value)}
                          disabled={creatingProxy || providerOptions.length === 0}
                        >
                          {providerOptions.length === 0 ? (
                            <option value="">No providers</option>
                          ) : (
                            providerOptions.map((provider) => (
                              <option key={provider} value={provider}>
                                {provider}
                              </option>
                            ))
                          )}
                        </select>
                      </label>
                      <label className="form-field">
                        <span>Location</span>
                        <select
                          className="input"
                          value={selectedCreateLocation}
                          onChange={(e) => handleCreateProxyLocationChange(e.target.value)}
                          disabled={creatingProxy || locationOptions.length === 0}
                        >
                          {locationOptions.length === 0 ? (
                            <option value="">No locations</option>
                          ) : (
                            locationOptions.map((location) => (
                              <option key={location} value={location}>
                                {location}
                              </option>
                            ))
                          )}
                        </select>
                      </label>
                    </div>
                    <label className="form-field">
                      <span>OVPN profile</span>
                      <select
                        className="input"
                        value={selectedCreateOvpn}
                        onChange={(e) => {
                          setCreateProxyOvpn(e.target.value);
                          setCreatedProxy(null);
                        }}
                        disabled={creatingProxy || locationRouteRows.length === 0}
                        required
                      >
                        {locationRouteRows.length === 0 ? (
                          <option value="">No OVPN profiles</option>
                        ) : (
                          locationRouteRows.map((row) => (
                            <option key={row.file} value={row.file}>
                              {row.file}
                            </option>
                          ))
                        )}
                      </select>
                    </label>
                  </>
                )}
                <div className="form-grid">
                  <label className="form-field">
                    <span>Proxy type</span>
                    <select
                      className="input"
                      value={createProxyType}
                      onChange={(e) => {
                        setCreateProxyType(e.target.value === 'socks5' ? 'socks5' : 'http');
                        setCreatedProxy(null);
                      }}
                      disabled={creatingProxy}
                    >
                      <option value="http">HTTP</option>
                      <option value="socks5">SOCKS5</option>
                    </select>
                  </label>
                  <label className="form-field">
                    <span>Label</span>
                    <input
                      className="input"
                      value={createProxyLabel}
                      onChange={(e) => {
                        setCreateProxyLabel(e.target.value);
                        setCreatedProxy(null);
                      }}
                      placeholder={formatOvpnDisplayLabel(selectedCreateOvpn) || 'US Chicago'}
                      disabled={creatingProxy}
                    />
                  </label>
                  <label className="form-field">
                    <span>External ID</span>
                    <input
                      className="input"
                      value={createProxyExternalId}
                      onChange={(e) => {
                        setCreateProxyExternalId(e.target.value);
                        setCreatedProxy(null);
                      }}
                      placeholder="launcher-123"
                      disabled={creatingProxy}
                    />
                  </label>
                </div>
                {createProxyEgressType === 'ovpn' && (
                  <fieldset className="dashboard-rotation-fieldset" disabled={creatingProxy}>
                    <legend className="dashboard-modal-label">OpenVPN Rotation</legend>
                    <div className="dashboard-rotation-grid">
                      <label className="form-field">
                        <span>Interval minutes</span>
                        <input
                          type="number"
                          className="input"
                          min="0"
                          step="1"
                          value={createProxyRotationMinutes}
                          onChange={(e) => {
                            setCreateProxyRotationMinutes(e.target.value);
                            setCreatedProxy(null);
                          }}
                          placeholder="0"
                        />
                      </label>
                      <label className="form-field">
                        <span>Country pool</span>
                        <select
                          className="input"
                          value={createProxyRotationCountry}
                          onChange={(e) => {
                            setCreateProxyRotationCountry(e.target.value);
                            setCreatedProxy(null);
                          }}
                          disabled={creatingProxy || Number(createProxyRotationMinutes) <= 0}
                        >
                          <option value="">Use global default</option>
                          {ovpnCountries.map((country) => (
                            <option key={country.code} value={country.code}>
                              {country.label}
                              {typeof country.count === 'number' ? ` (${country.count})` : ''}
                            </option>
                          ))}
                        </select>
                      </label>
                    </div>
                  </fieldset>
                )}
                <div className="dashboard-generated-preview">
                  <span className="dashboard-modal-label">Username preview</span>
                  <code className="text-mono">{createProxyUsernamePreview}</code>
                </div>
                {createdProxy && (
                  <div className="dashboard-created-proxy">
                    <div className="dashboard-created-proxy-title">
                      <span className="material-symbols-outlined">check_circle</span>
                      <strong>{createdProxy.username}</strong>
                    </div>
                    {(() => {
                      const createdPort = createdProxy.proxyType === 'socks5' ? socksPort : httpPort;
                      const createdColon = routeColonFormat(createdProxy.username, createdPort);
                      const createdAt = routeAtFormat(createdProxy.username, createdPort);
                      return (
                        <>
                          <button
                            type="button"
                            className="dashboard-copy-line"
                            onClick={() => copyProxyLine(createdColon, `created-colon-${createdProxy.username}`)}
                          >
                            <code className="dashboard-copy-code">{createdColon}</code>
                            {copiedToken === `created-colon-${createdProxy.username}` && <span className="dashboard-copy-toast">Copied</span>}
                          </button>
                          <button
                            type="button"
                            className="dashboard-copy-line"
                            onClick={() => copyProxyLine(createdAt, `created-at-${createdProxy.username}`)}
                          >
                            <code className="dashboard-copy-code">{createdAt}</code>
                            {copiedToken === `created-at-${createdProxy.username}` && <span className="dashboard-copy-toast">Copied</span>}
                          </button>
                        </>
                      );
                    })()}
                  </div>
                )}
                <div className="dashboard-modal-actions">
                  <button
                    type="submit"
                    className="btn-primary dashboard-modal-submit"
                    disabled={creatingProxy || !canCreateProxy}
                  >
                    {creatingProxy ? 'Creating...' : 'Create Proxy'}
                  </button>
                </div>
              </form>
            </div>
          </div>
        )}

        {showAuthRouteEditor && (
          <div className="dashboard-modal-overlay" onClick={() => setShowAuthRouteEditor(false)}>
            <div
              className="dashboard-modal-panel dashboard-route-editor-panel"
              onClick={(e) => e.stopPropagation()}
              role="dialog"
              aria-modal="true"
              aria-labelledby="dashboard-route-editor-title"
            >
              <div className="dashboard-modal-header">
                <div className="flex items-center gap-2">
                  <span className="material-symbols-outlined text-primary">tune</span>
                  <h3 id="dashboard-route-editor-title" className="dashboard-modal-title">
                    Edit Route
                  </h3>
                </div>
                <button
                  type="button"
                  onClick={() => setShowAuthRouteEditor(false)}
                  className="dashboard-modal-close"
                  aria-label="Close"
                >
                  <span className="material-symbols-outlined">close</span>
                </button>
              </div>
              <form className="dashboard-modal-body" onSubmit={saveAuthRoute}>
                <div className="form-grid">
                  <label className="form-field">
                    <span>Username</span>
                    <input
                      className="input"
                      value={authRouteForm.username}
                      onChange={(e) => setAuthRouteForm((prev) => ({ ...prev, username: e.target.value }))}
                      placeholder="us_chicago"
                    />
                  </label>
                  <label className="form-field">
                    <span>Label</span>
                    <input
                      className="input"
                      value={authRouteForm.label}
                      onChange={(e) => setAuthRouteForm((prev) => ({ ...prev, label: e.target.value }))}
                      placeholder="US Chicago"
                    />
                  </label>
                  <label className="form-field">
                    <span>External ID</span>
                    <input
                      className="input"
                      value={authRouteForm.externalId}
                      onChange={(e) => setAuthRouteForm((prev) => ({ ...prev, externalId: e.target.value }))}
                      placeholder="launcher-123"
                    />
                  </label>
                  <label className="form-field">
                    <span>Proxy type</span>
                    <select
                      className="input"
                      value={authRouteForm.proxyType}
                      onChange={(e) => setAuthRouteForm((prev) => ({ ...prev, proxyType: e.target.value === 'socks5' ? 'socks5' : 'http' }))}
                    >
                      <option value="http">HTTP</option>
                      <option value="socks5">SOCKS5</option>
                    </select>
                  </label>
                  <label className="form-field">
                    <span>Egress</span>
                    <select
                      className="input"
                      value={authRouteForm.egressType}
                      onChange={(e) => {
                        const nextType = e.target.value === 'upstream' ? 'upstream' : 'ovpn';
                        setAuthRouteForm((prev) => ({
                          ...prev,
                          egressType: nextType,
                          rotationMinutes: nextType === 'ovpn' ? prev.rotationMinutes : '0',
                          rotationCountry: nextType === 'ovpn' ? prev.rotationCountry : '',
                        }));
                      }}
                    >
                      <option value="ovpn">OpenVPN</option>
                      <option value="upstream">Upstream proxy</option>
                    </select>
                  </label>
                  {authRouteForm.egressType === 'ovpn' ? (
                    <label className="form-field">
                      <span>OVPN profile</span>
                      <OvpnFileSelect
                        files={sortedOvpnFiles}
                        value={authRouteForm.ovpn}
                        onChange={(file) => setAuthRouteForm((prev) => ({ ...prev, ovpn: file }))}
                        placeholder="Select profile..."
                      />
                    </label>
                  ) : (
                    <label className="form-field">
                      <span>Upstream proxy</span>
                      <select
                        className="input"
                        value={authRouteForm.upstreamProxyId}
                        onChange={(e) => setAuthRouteForm((prev) => ({ ...prev, upstreamProxyId: e.target.value }))}
                      >
                        <option value="">Select upstream...</option>
                        {upstreamProxies.map((proxy) => (
                          <option key={proxy.id} value={proxy.id}>
                            {proxy.label || proxy.host || proxy.id}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {authRouteForm.egressType === 'ovpn' && (
                    <fieldset className="dashboard-rotation-fieldset dashboard-auth-rotation-fieldset">
                      <legend className="dashboard-modal-label">OpenVPN Rotation</legend>
                      <div className="dashboard-rotation-grid">
                        <label className="form-field">
                          <span>Interval minutes</span>
                          <input
                            type="number"
                            className="input"
                            min="0"
                            step="1"
                            value={authRouteForm.rotationMinutes}
                            onChange={(e) =>
                              setAuthRouteForm((prev) => ({
                                ...prev,
                                rotationMinutes: e.target.value,
                              }))
                            }
                            placeholder="0"
                          />
                        </label>
                        <label className="form-field">
                          <span>Country pool</span>
                          <select
                            className="input"
                            value={authRouteForm.rotationCountry}
                            onChange={(e) =>
                              setAuthRouteForm((prev) => ({
                                ...prev,
                                rotationCountry: e.target.value,
                              }))
                            }
                            disabled={Number(authRouteForm.rotationMinutes) <= 0}
                          >
                            <option value="">Use global default</option>
                            {ovpnCountries.map((country) => (
                              <option key={country.code} value={country.code}>
                                {country.label}
                                {typeof country.count === 'number' ? ` (${country.count})` : ''}
                              </option>
                            ))}
                          </select>
                        </label>
                      </div>
                    </fieldset>
                  )}
                  <label className="form-field flex-row items-center gap-2">
                    <input
                      type="checkbox"
                      checked={authRouteForm.enabled}
                      onChange={(e) => setAuthRouteForm((prev) => ({ ...prev, enabled: e.target.checked }))}
                    />
                    <span>Enabled</span>
                  </label>
                </div>
                <div className="dashboard-row-actions justify-end">
                  <button type="button" className="btn-secondary" onClick={() => setShowAuthRouteEditor(false)}>
                    Cancel
                  </button>
                  <button type="button" className="btn-secondary" onClick={resetAuthRouteForm}>
                    Clear
                  </button>
                  <button type="submit" className="btn-primary" disabled={Boolean(authRouteBusy)}>
                    {authRouteBusy?.startsWith('save:') ? 'Saving...' : 'Save Route'}
                  </button>
                </div>
              </form>
            </div>
          </div>
        )}
      </div>
    );
  }

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
                  <th>
                    <span className="block">scheme://user:pass@host:port</span>
                    <span className="text-muted text-xs font-normal">URL</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {runningProxyRows.map((row) => {
                  const tokColon = `colon-${row.internalPort}`;
                  const tokAt = `at-${row.internalPort}`;
                  const tokUrl = `url-${row.internalPort}`;
                  return (
                    <tr key={row.internalPort}>
                      <td className="font-medium" data-label="Location">{row.label}</td>
                      <td data-label="Host port">
                        <div className="flex flex-col gap-1 items-start">
                          <span className="badge-outline">
                            {row.proxyType === 'socks5' ? 'SOCKS5' : 'HTTP'}
                          </span>
                          <span className="text-primary text-mono font-bold">{row.hostPort}</span>
                        </div>
                      </td>
                      <td data-label="Colon format">
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
                      <td data-label="At format">
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
                      <td data-label="URL">
                        <button
                          type="button"
                          className="dashboard-copy-line"
                          title="Click to copy"
                          onClick={() => copyProxyLine(row.schemeUrl, tokUrl)}
                        >
                          <code className="dashboard-copy-code">{row.schemeUrl}</code>
                          {copiedToken === tokUrl && (
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
            onClick={() => {
              if (!showCreateEntry) {
                setNewEntryProxyType('http');
              }
              setShowCreateEntry(!showCreateEntry);
            }}
          >
            <span className="material-symbols-outlined">{showCreateEntry ? 'close' : 'add'}</span>
            {showCreateEntry ? 'Cancel' : 'Create Entry'}
          </button>
        </div>

        {selectedTablePorts.length > 0 && (
          <div className="dashboard-batch-bar flex flex-wrap items-center gap-2 px-4 py-3 border-t border-[var(--border-color)]">
            <span className="text-sm font-medium">
              {selectedTablePorts.length} selected
              {effectiveSelectedPorts.length !== selectedTablePorts.length && (
                <span className="text-muted font-normal">
                  {' '}
                  ({effectiveSelectedPorts.length} apply; others are not in this list)
                </span>
              )}
            </span>
            <button
              type="button"
              className="btn-secondary text-sm"
              disabled={batchBusy || effectiveSelectedPorts.length === 0}
              onClick={() => batchApplyProxyType(configuredInternalPorts, 'http')}
            >
              Set HTTP
            </button>
            <button
              type="button"
              className="btn-secondary text-sm"
              disabled={batchBusy || effectiveSelectedPorts.length === 0}
              onClick={() => batchApplyProxyType(configuredInternalPorts, 'socks5')}
            >
              Set SOCKS5
            </button>
            <button
              type="button"
              className="btn-danger text-sm"
              disabled={batchBusy || effectiveSelectedPorts.length === 0}
              onClick={() => batchDeleteSelected(configuredInternalPorts)}
            >
              Delete selected
            </button>
            <button
              type="button"
              className="btn-secondary text-sm"
              disabled={batchBusy}
              onClick={() => setSelectedTablePorts([])}
            >
              Clear selection
            </button>
          </div>
        )}

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
                  <span className="dashboard-modal-label">Egress type</span>
                  <select
                    className="dashboard-modal-input"
                    value={newEntryEgressType}
                    onChange={(e) => setNewEntryEgressType(e.target.value === 'upstream' ? 'upstream' : 'ovpn')}
                    disabled={creatingEntry}
                  >
                    <option value="ovpn">OpenVPN</option>
                    <option value="upstream">Upstream Proxy</option>
                  </select>
                </label>
                {newEntryEgressType === 'upstream' && (
                  <label className="dashboard-modal-field">
                    <span className="dashboard-modal-label">Saved upstream proxy</span>
                    <select
                      className="dashboard-modal-input"
                      value={newEntryUpstreamProxyId}
                      onChange={(e) => setNewEntryUpstreamProxyId(e.target.value)}
                      disabled={creatingEntry}
                    >
                      <option value="">Select upstream proxy...</option>
                      {upstreamProxies.map((proxy) => (
                        <option key={proxy.id} value={proxy.id}>
                          {proxy.label} ({proxy.scheme}://{proxy.host}:{proxy.port})
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                {newEntryEgressType === 'ovpn' && (
                <label className="dashboard-modal-field">
                  <span className="dashboard-modal-label">Location Configuration (Optional)</span>
                  <OvpnFileSelect
                    files={sortedOvpnFiles}
                    value={newEntryOvpn}
                    onChange={setNewEntryOvpn}
                    disabled={creatingEntry}
                    placeholder="Select location .ovpn..."
                  />
                </label>
                )}
                <label className="dashboard-modal-field">
                  <span className="dashboard-modal-label">Proxy type</span>
                  <select
                    className="dashboard-modal-input dashboard-proxy-type-select"
                    value={newEntryProxyType}
                    onChange={(e) => setNewEntryProxyType(e.target.value === 'socks5' ? 'socks5' : 'http')}
                    disabled={creatingEntry}
                    aria-label="Proxy type for new entries"
                  >
                    <option value="http">HTTP</option>
                    <option value="socks5">SOCKS5</option>
                  </select>
                </label>
                {newEntryEgressType === 'upstream' && (
                  <fieldset className="dashboard-rotation-fieldset" disabled={creatingEntry}>
                    <legend className="dashboard-modal-label">Upstream Refresh</legend>
                    <label className="dashboard-modal-field">
                      <span className="dashboard-modal-label">Interval (minutes, 0 = off)</span>
                      <input
                        type="number"
                        className="dashboard-modal-input"
                        min="0"
                        step="1"
                        value={newEntryUpstreamRefreshMinutes}
                        onChange={(e) => setNewEntryUpstreamRefreshMinutes(e.target.value)}
                      />
                    </label>
                  </fieldset>
                )}
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
        
        {showEditEntry && (
          <div className="dashboard-modal-overlay" onClick={() => setShowEditEntry(false)}>
            <div
              className="dashboard-modal-panel glass-panel"
              onClick={(e) => e.stopPropagation()}
              role="dialog"
              aria-modal="true"
            >
              <div className="dashboard-modal-header">
                <h3 className="dashboard-modal-title">Edit Entry Configuration</h3>
                <button
                  type="button"
                  onClick={() => setShowEditEntry(false)}
                  className="dashboard-modal-close"
                  aria-label="Close"
                >
                  <span className="material-symbols-outlined">close</span>
                </button>
              </div>
              <form className="dashboard-modal-body" onSubmit={handleEditEntrySubmit}>
                <label className="dashboard-modal-field">
                  <span className="dashboard-modal-label">Launcher ID</span>
                  <input
                    type="text"
                    className="dashboard-modal-input"
                    value={editEntryId}
                    onChange={(e) => setEditEntryId(e.target.value)}
                    placeholder="Enter a unique ID…"
                    required
                  />
                </label>
                <label className="dashboard-modal-field">
                  <span className="dashboard-modal-label">Egress type</span>
                  <select
                    className="dashboard-modal-input"
                    value={editEntryEgressType}
                    onChange={(e) => setEditEntryEgressType(e.target.value === 'upstream' ? 'upstream' : 'ovpn')}
                    disabled={isEditingEntry}
                  >
                    <option value="ovpn">OpenVPN</option>
                    <option value="upstream">Upstream Proxy</option>
                  </select>
                </label>
                {editEntryEgressType === 'upstream' && (
                  <label className="dashboard-modal-field">
                    <span className="dashboard-modal-label">Saved upstream proxy</span>
                    <select
                      className="dashboard-modal-input"
                      value={editEntryUpstreamProxyId}
                      onChange={(e) => setEditEntryUpstreamProxyId(e.target.value)}
                      disabled={isEditingEntry}
                    >
                      <option value="">Select upstream proxy...</option>
                      {upstreamProxies.map((proxy) => (
                        <option key={proxy.id} value={proxy.id}>
                          {proxy.label} ({proxy.scheme}://{proxy.host}:{proxy.port})
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                {editEntryEgressType === 'ovpn' && (
                <label className="dashboard-modal-field">
                  <span className="dashboard-modal-label">Location Configuration</span>
                  <OvpnFileSelect
                    files={sortedOvpnFiles}
                    value={editEntryOvpn}
                    onChange={setEditEntryOvpn}
                    disabled={isEditingEntry}
                    placeholder="Select location .ovpn..."
                  />
                </label>
                )}
                <label className="dashboard-modal-field">
                  <span className="dashboard-modal-label">Proxy type</span>
                  <select
                    className="dashboard-modal-input dashboard-proxy-type-select"
                    value={editEntryProxyType}
                    onChange={(e) => setEditEntryProxyType(e.target.value === 'socks5' ? 'socks5' : 'http')}
                    disabled={isEditingEntry}
                    aria-label="Proxy type for this port"
                  >
                    <option value="http">HTTP</option>
                    <option value="socks5">SOCKS5</option>
                  </select>
                </label>
                {editEntryEgressType === 'upstream' && (
                  <fieldset className="dashboard-rotation-fieldset" disabled={isEditingEntry}>
                    <legend className="dashboard-modal-label">Upstream Refresh</legend>
                    <label className="dashboard-modal-field">
                      <span className="dashboard-modal-label">Interval (minutes, 0 = off)</span>
                      <input
                        type="number"
                        className="dashboard-modal-input"
                        min="0"
                        step="1"
                        value={editEntryUpstreamRefreshMinutes}
                        onChange={(e) => setEditEntryUpstreamRefreshMinutes(e.target.value)}
                      />
                    </label>
                  </fieldset>
                )}
                <div className="dashboard-modal-actions">
                  <button
                    type="submit"
                    className="btn-primary dashboard-modal-submit"
                    disabled={isEditingEntry || !editEntryId.trim()}
                  >
                    {isEditingEntry ? 'Saving...' : 'Save Changes'}
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
                <th style={{ width: '36px', textAlign: 'center' }} aria-label="Select rows">
                  <input
                    type="checkbox"
                    checked={allVisibleSelected}
                    disabled={batchBusy || visibleInternalPorts.length === 0}
                    onChange={() => {
                      if (allVisibleSelected) {
                        setSelectedTablePorts((prev) =>
                          prev.filter((p) => !visibleInternalPorts.includes(p)),
                        );
                      } else {
                        setSelectedTablePorts((prev) => {
                          const next = new Set([...prev, ...visibleInternalPorts]);
                          return [...next];
                        });
                      }
                    }}
                  />
                </th>
                <th style={{ width: '40px', textAlign: 'center' }}>#</th>
                <th
                  scope="col"
                  aria-sort={
                    launcherTableSort.key === 'id'
                      ? launcherTableSort.dir === 'asc'
                        ? 'ascending'
                        : 'descending'
                      : 'none'
                  }
                >
                  <button
                    type="button"
                    className="dashboard-sort-header"
                    onClick={() =>
                      setLauncherTableSort((prev) =>
                        prev.key !== 'id'
                          ? { key: 'id', dir: 'asc' }
                          : { key: 'id', dir: prev.dir === 'asc' ? 'desc' : 'asc' },
                      )
                    }
                  >
                    <span>ID</span>
                    {launcherTableSort.key === 'id' && (
                      <span className="material-symbols-outlined dashboard-sort-header-icon" aria-hidden>
                        {launcherTableSort.dir === 'asc' ? 'arrow_upward' : 'arrow_downward'}
                      </span>
                    )}
                  </button>
                </th>
                <th>{portColumnLabel}</th>
                <th
                  scope="col"
                  aria-sort={
                    launcherTableSort.key === 'ovpn'
                      ? launcherTableSort.dir === 'asc'
                        ? 'ascending'
                        : 'descending'
                      : 'none'
                  }
                >
                  <button
                    type="button"
                    className="dashboard-sort-header"
                    onClick={() =>
                      setLauncherTableSort((prev) =>
                        prev.key !== 'ovpn'
                          ? { key: 'ovpn', dir: 'asc' }
                          : { key: 'ovpn', dir: prev.dir === 'asc' ? 'desc' : 'asc' },
                      )
                    }
                  >
                    <span>Egress</span>
                    {launcherTableSort.key === 'ovpn' && (
                      <span className="material-symbols-outlined dashboard-sort-header-icon" aria-hidden>
                        {launcherTableSort.dir === 'asc' ? 'arrow_upward' : 'arrow_downward'}
                      </span>
                    )}
                  </button>
                </th>
                <th>Status</th>
                <th className="text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {totalPorts === 0 || locations.length === 0 ? (
                <tr>
                  <td colSpan="7" className="text-center p-6 text-muted">No locations configured.</td>
                </tr>
              ) : filteredPortRows.length === 0 ? (
                <tr>
                  <td colSpan="7" className="text-center p-6 text-muted">
                    No ports match this ID search.
                  </td>
                </tr>
              ) : (
                launcherTableSortedRows.map(({ loc, idx }, arrayIndex) => {
                  const port = internalPortForIndex(status, idx);
                  const displayPort = publishedPortForIndex(status, idx);
                  const portKey = String(port);
                  const selected = selectedByPort[portKey] || '';
                  const egress = egressForPort(portKey);
                  const hasEgress = egress.type !== 'none';
                  const egressLabel = egressDisplay(egress);
                  const launcherIdServer = typeof loc.launcherId === 'string' ? loc.launcherId : '';
                  const proxyTypeServer = loc.proxyType === 'socks5' ? 'socks5' : 'http';
                  const rotationMinutes = Math.max(0, Math.floor(Number(loc.rotationIntervalMinutes) || 0));
                  const rotationCountry = (loc.rotationCountry || '').toUpperCase();
                  const isRotating = rotationMinutes > 0;
                  const refreshMinutes = Math.max(0, Math.floor(Number(loc.upstreamRefreshIntervalMinutes) || 0));
                  const activationState = activationStateByPort[portKey] || (enabledPorts.has(port) ? 'active' : 'inactive');
                  const isStarting = activationState === 'starting';
                  const isActive = activationState === 'active';
                  const isFailed = activationState === 'failed';
                  const canStart = !isStarting && (hasEgress || isRotating);
                  const rowChecked = selectedTablePorts.includes(port);
                  return (
                    <tr key={port} className={hasEgress ? 'dashboard-row-ovpn-selected' : undefined}>
                      <td className="text-center align-middle" data-label="Select">
                        <input
                          type="checkbox"
                          checked={rowChecked}
                          disabled={batchBusy || busyPort === port}
                          onChange={() => {
                            setSelectedTablePorts((prev) =>
                              prev.includes(port) ? prev.filter((x) => x !== port) : [...prev, port],
                            );
                          }}
                          aria-label={`Select row ${launcherIdServer || displayPort}`}
                        />
                      </td>
                      <td className="text-muted text-sm font-bold text-center border-r border-[var(--border-color)]" style={{ opacity: 0.5 }} data-label="#">
                        {arrayIndex + 1}
                      </td>
                      <td className="dashboard-launcher-id-cell" data-label="ID">
                        <div
                          className="dashboard-copy-line text-mono"
                          style={{ padding: '0.45rem 0.55rem', border: '1px solid var(--border-color)', background: 'rgba(0,0,0,0.2)' }}
                          title="Click to copy ID"
                          onClick={() => {
                            if (!launcherIdServer) return;
                            copyProxyLine(launcherIdServer, `id-${port}`);
                          }}
                        >
                          <span className="dashboard-copy-code" style={{ fontSize: '0.85rem' }}>
                            {launcherIdServer || '—'}
                          </span>
                          {copiedToken === `id-${port}` && (
                            <span className="dashboard-copy-toast">Copied!</span>
                          )}
                        </div>
                      </td>
                      <td className="text-primary font-bold" data-label={portColumnLabel}>
                        <div className="flex flex-col gap-1 items-start">
                          <span className="badge-outline">
                            {proxyTypeServer === 'socks5' ? 'SOCKS5' : 'HTTP'}
                          </span>
                          <button
                            type="button"
                            className="dashboard-copy-line text-mono"
                            title="Click to copy host port"
                            onClick={() => {
                              copyProxyLine(String(displayPort), `hostport-${port}`);
                            }}
                          >
                            <span className="dashboard-copy-code">{displayPort}</span>
                            {copiedToken === `hostport-${port}` && (
                              <span className="dashboard-copy-toast">Copied!</span>
                            )}
                          </button>
                        </div>
                      </td>
                      <td data-label="Egress">
                        <div className="dashboard-ovpn-cell">
                          <span className="dashboard-egress-kind">
                            {egress.type === 'upstream'
                              ? 'Upstream Proxy'
                              : egress.type === 'ovpn'
                                ? 'OpenVPN'
                                : 'No egress'}
                          </span>
                          {egress.type === 'upstream' && (
                            <span className="text-mono text-sm">{egressLabel || 'Missing upstream profile'}</span>
                          )}
                          {egress.type !== 'upstream' && (
                            <OvpnFileSelect
                              files={sortedOvpnFiles}
                              value={selected}
                              onChange={(file) => onSelectRowFile(port, file)}
                              disabled={busyPort === port || isStarting}
                              placeholder={isRotating ? 'Rotating…' : 'Select profile…'}
                            />
                          )}
                          {egress.type !== 'upstream' && (
                            <select
                              className="dashboard-location-country-select"
                              value=""
                              onChange={(e) => {
                                const country = e.target.value;
                                if (country) changePortLocation(port, { country });
                              }}
                              disabled={busyPort === port || isStarting || ovpnFiles.length === 0}
                              title={
                                isActive
                                  ? 'Change country and restart this proxy on the same port'
                                  : 'Save a random profile from this country'
                              }
                              aria-label="Change port country"
                            >
                              <option value="">Change country...</option>
                              <option value="random">Random any country</option>
                              {ovpnCountries.map((c) => (
                                <option key={c.code} value={c.code} disabled={!c.count}>
                                  {c.label}
                                  {typeof c.count === 'number' ? ` (${c.count})` : ''}
                                </option>
                              ))}
                            </select>
                          )}
                          {isRotating && (
                            <span
                              className="dashboard-rotation-badge"
                              title={`Rotates every ${rotationMinutes}m${
                                rotationCountry ? ` from ${rotationCountry} pool` : ''
                              }`}
                            >
                              <span className="material-symbols-outlined" aria-hidden>
                                autorenew
                              </span>
                              <span>
                                Rotating - {rotationMinutes}m
                                {rotationCountry ? ` - ${rotationCountry}` : ''}
                              </span>
                            </span>
                          )}
                          {egress.type === 'upstream' && refreshMinutes > 0 && (
                            <span
                              className="dashboard-rotation-badge"
                              title={`Refreshes the same upstream proxy every ${refreshMinutes}m`}
                            >
                              <span className="material-symbols-outlined" aria-hidden>
                                restart_alt
                              </span>
                              <span>Refresh {refreshMinutes}m</span>
                            </span>
                          )}
                        </div>
                      </td>
                      <td data-label="Status">
                        <span className={isActive ? 'status-active' : isStarting ? 'status-starting' : isFailed ? 'status-failed' : 'status-inactive'}>
                          {isActive ? 'Active' : isStarting ? 'Starting...' : isFailed ? 'Failed' : 'Inactive'}
                        </span>
                        {isFailed && activationErrorByPort[portKey] && (
                          <div className="status-error-text">{activationErrorByPort[portKey]}</div>
                        )}
                      </td>
                      <td className="text-right" data-label="Actions">
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
                                className="btn-secondary"
                                title="Edit Configuration"
                                disabled={busyPort === port || isStarting}
                                onClick={() => openEditModal(port, launcherIdServer, egress, proxyTypeServer, refreshMinutes)}
                                style={{ padding: '0.4rem', display: 'flex', alignItems: 'center' }}
                              >
                                <span className="material-symbols-outlined" style={{ fontSize: '1rem' }}>edit</span>
                              </button>
                              <button
                                type="button"
                                className="btn-danger"
                                title="Delete this entry completely"
                                disabled={busyPort === port}
                                onClick={() => deleteEntry(port)}
                                style={{ padding: '0.4rem', display: 'flex', alignItems: 'center' }}
                              >
                                <span className="material-symbols-outlined" style={{ fontSize: '1rem' }}>delete</span>
                              </button>
                            </>
                          ) : (
                            <>
                              <button
                                type="button"
                                className="btn-secondary"
                                disabled={busyPort === port || isStarting}
                                onClick={() => extendPort(port)}
                                title="Add 30 minutes before idle auto-close"
                              >
                                {busyPort === port ? 'Working...' : '+30m'}
                              </button>
                              <button
                                type="button"
                                className="btn-secondary"
                                title="Restart proxy (reload VPN / worker)"
                                aria-label="Restart proxy"
                                disabled={busyPort === port || isStarting || !hasEgress}
                                onClick={() =>
                                  restartPort(port, { hasEgress })
                                }
                                style={{ padding: '0.4rem', display: 'flex', alignItems: 'center' }}
                              >
                                <span className="material-symbols-outlined" style={{ fontSize: '1rem' }} aria-hidden>
                                  restart_alt
                                </span>
                              </button>
                              <button
                                type="button"
                                className="btn-danger"
                                disabled={busyPort === port || isStarting}
                                onClick={() => setActivation(port, false)}
                              >
                                {busyPort === port ? 'Working...' : 'Stop'}
                              </button>
                              <button
                                type="button"
                                className="btn-secondary"
                                title="Edit Configuration"
                                disabled={busyPort === port || isStarting}
                                onClick={() => openEditModal(port, launcherIdServer, egress, proxyTypeServer, refreshMinutes)}
                                style={{ padding: '0.4rem', display: 'flex', alignItems: 'center' }}
                              >
                                <span className="material-symbols-outlined" style={{ fontSize: '1rem' }}>edit</span>
                              </button>
                              <button
                                type="button"
                                className="btn-danger"
                                title="Delete this entry completely"
                                disabled={busyPort === port || isStarting}
                                onClick={() => deleteEntry(port)}
                                style={{ padding: '0.4rem', display: 'flex', alignItems: 'center', backgroundColor: '#e74c3c' }}
                              >
                                <span className="material-symbols-outlined" style={{ fontSize: '1rem' }}>delete</span>
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
