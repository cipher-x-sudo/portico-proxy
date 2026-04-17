/**
 * Short label for UI (full filename remains the value sent to the API).
 */
export function formatOvpnDisplayLabel(filename) {
  if (!filename) return '';
  return filename
    .replace(/\.protonvpn\.(tcp|udp)\.ovpn$/i, '')
    .replace(/\.ovpn$/i, '');
}

/** Compact label for dropdown trigger (US city/state, Proton id/region, else basename). */
export function formatOvpnRichLabel(filename) {
  if (!filename) return '';
  const us = parseUnitedStatesOvpnMeta(filename);
  if (us) return `${us.state} · ${us.city}`;
  const proton = parseProtonOvpnMeta(filename);
  if (proton) return `${proton.id} · ${proton.country} ${proton.region}`;
  return formatOvpnDisplayLabel(filename);
}

/**
 * Proton-style: 118-us-az.protonvpn.tcp.ovpn
 */
export function parseProtonOvpnMeta(filename) {
  const m = filename.match(
    /^(\d+)-([a-z]{2})-([a-z0-9_-]+)\.protonvpn\.(tcp|udp)\.ovpn$/i
  );
  if (!m) return null;
  return {
    id: m[1],
    country: m[2].toUpperCase(),
    region: m[3].replace(/_/g, ' ').toUpperCase(),
    proto: m[4].toLowerCase(),
  };
}

/** Same slugs as backend/ovpn_filter.py; longest match first for state prefix. */
const US_STATE_SLUGS_RAW = [
  'ALABAMA',
  'ALASKA',
  'ARIZONA',
  'ARKANSAS',
  'CALIFORNIA',
  'COLORADO',
  'CONNECTICUT',
  'DELAWARE',
  'DISTRICT_OF_COLUMBIA',
  'FLORIDA',
  'GEORGIA',
  'HAWAII',
  'IDAHO',
  'ILLINOIS',
  'INDIANA',
  'IOWA',
  'KANSAS',
  'KENTUCKY',
  'LOUISIANA',
  'MAINE',
  'MARYLAND',
  'MASSACHUSETTS',
  'MICHIGAN',
  'MINNESOTA',
  'MISSISSIPPI',
  'MISSOURI',
  'MONTANA',
  'NEBRASKA',
  'NEVADA',
  'NEW_HAMPSHIRE',
  'NEW_JERSEY',
  'NEW_MEXICO',
  'NEW_YORK',
  'NORTH_CAROLINA',
  'NORTH_DAKOTA',
  'OHIO',
  'OKLAHOMA',
  'OREGON',
  'PENNSYLVANIA',
  'RHODE_ISLAND',
  'SOUTH_CAROLINA',
  'SOUTH_DAKOTA',
  'TENNESSEE',
  'TEXAS',
  'UTAH',
  'VERMONT',
  'VIRGINIA',
  'WASHINGTON',
  'WEST_VIRGINIA',
  'WISCONSIN',
  'WYOMING',
];
const US_STATE_SLUGS_SORTED = [...US_STATE_SLUGS_RAW].sort((a, b) => b.length - a.length);

function humanizeSlug(slug) {
  return slug
    .split('_')
    .map((w) => w.charAt(0) + w.slice(1).toLowerCase())
    .join(' ');
}

/**
 * United_States_<StateSlug>_<CitySlug>.ovpn
 */
export function parseUnitedStatesOvpnMeta(filename) {
  if (!filename.startsWith('United_States_') || !filename.endsWith('.ovpn')) {
    return null;
  }
  const rest = filename.slice('United_States_'.length, -'.ovpn'.length);
  const restU = rest.toUpperCase();
  for (const stateSlug of US_STATE_SLUGS_SORTED) {
    const prefix = `${stateSlug}_`;
    if (restU.startsWith(prefix)) {
      const citySlug = rest.slice(prefix.length);
      if (!citySlug) return null;
      return {
        state: humanizeSlug(stateSlug),
        city: humanizeSlug(citySlug),
        stateSlug,
        citySlug,
      };
    }
  }
  return null;
}

function leadingNumericId(name) {
  const m = String(name).match(/^(\d+)/);
  return m ? parseInt(m[1], 10) : null;
}

function unitedStatesSortKey(filename) {
  const u = parseUnitedStatesOvpnMeta(filename);
  if (!u) return null;
  return `${u.stateSlug}\0${u.citySlug}`;
}

/**
 * United_States_*: state then city. Proton-style: numeric ID. Else locale.
 */
export function sortOvpnFiles(files) {
  if (!Array.isArray(files)) return [];
  return [...files].sort((a, b) => {
    const ka = unitedStatesSortKey(a);
    const kb = unitedStatesSortKey(b);
    if (ka && kb) {
      return ka.localeCompare(kb, undefined, { sensitivity: 'base', numeric: true });
    }
    if (ka && !kb) return -1;
    if (!ka && kb) return 1;
    const na = leadingNumericId(a);
    const nb = leadingNumericId(b);
    if (na != null && nb != null && na !== nb) return na - nb;
    if (na != null && nb == null) return -1;
    if (na == null && nb != null) return 1;
    return a.localeCompare(b, undefined, { sensitivity: 'base', numeric: true });
  });
}

/** Lowercase string used for search (filename, label, id, country, region, state, city). */
export function ovpnFileSearchHaystack(filename) {
  if (!filename) return '';
  const parts = [filename, formatOvpnDisplayLabel(filename)];
  const meta = parseProtonOvpnMeta(filename);
  if (meta) {
    parts.push(
      meta.id,
      meta.country,
      meta.region,
      meta.proto,
      `${meta.country} ${meta.region}`,
      `${meta.id} ${meta.country}`,
      `${meta.id}-${meta.country}`.toLowerCase()
    );
  }
  const us = parseUnitedStatesOvpnMeta(filename);
  if (us) {
    parts.push(
      us.state.toLowerCase(),
      us.city.toLowerCase(),
      `${us.state} ${us.city}`.toLowerCase(),
      us.stateSlug.toLowerCase().replace(/_/g, ' '),
      us.citySlug.toLowerCase().replace(/_/g, ' '),
      us.stateSlug.toLowerCase(),
      us.citySlug.toLowerCase()
    );
  }
  return parts.join(' ').toLowerCase();
}

/**
 * Filter by query: whitespace-separated tokens; each token must appear in the haystack (substring).
 */
export function filterOvpnFilesByQuery(files, query) {
  if (!Array.isArray(files)) return [];
  const raw = String(query).trim().toLowerCase();
  if (!raw) return files;
  const tokens = raw.split(/\s+/).filter(Boolean);
  return files.filter((f) => {
    const hay = ovpnFileSearchHaystack(f);
    return tokens.every((t) => hay.includes(t));
  });
}
