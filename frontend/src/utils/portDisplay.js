/**
 * Map internal gateway listener port (portBase + index) to host-published port when
 * Docker maps e.g. 58000:50000 (set PUBLISHED_PROXY_PORT_BASE on gateway to match compose host mapping).
 */
export function publishedPortForIndex(status, index) {
  if (!status) return null;
  const internal = status.portBase + index;
  const pb = status.publishedPortBase;
  if (pb == null || typeof pb !== 'number') return internal;
  return pb + index;
}

/** Internal listener port for API ?port= */
export function internalPortForIndex(status, index) {
  if (!status) return null;
  return status.portBase + index;
}

export function internalToPublishedPort(status, internalPort) {
  if (!status || internalPort == null) return internalPort;
  const pb = status.publishedPortBase;
  if (pb == null || typeof pb !== 'number') return internalPort;
  return pb + (internalPort - status.portBase);
}
