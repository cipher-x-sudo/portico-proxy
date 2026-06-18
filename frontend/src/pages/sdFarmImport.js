async function getFileFromDirectoryHandle(dirHandle, parts) {
  let current = dirHandle;
  for (let i = 0; i < parts.length - 1; i += 1) {
    current = await current.getDirectoryHandle(parts[i]);
  }
  const fileHandle = await current.getFileHandle(parts[parts.length - 1]);
  return fileHandle.getFile();
}

async function findAccountsDbRecursive(dirHandle, depth = 0) {
  if (depth > 6) return null;
  for await (const entry of dirHandle.values()) {
    if (entry.kind === 'file' && entry.name.toLowerCase() === 'accounts.sqlite') {
      return entry.getFile();
    }
    if (entry.kind === 'directory') {
      const nested = await findAccountsDbRecursive(entry, depth + 1);
      if (nested) return nested;
    }
  }
  return null;
}

export async function findAccountsDbInDirectoryHandle(dirHandle) {
  try {
    return await getFileFromDirectoryHandle(dirHandle, ['DB', 'data', 'accounts.sqlite']);
  } catch {
    return findAccountsDbRecursive(dirHandle);
  }
}

export function findAccountsDbInWebkitFiles(files) {
  const normalized = (path) => String(path || '').replace(/\\/g, '/').toLowerCase();
  const exact = files.find((file) => normalized(file.webkitRelativePath).endsWith('db/data/accounts.sqlite'));
  if (exact) return exact;
  return files.find((file) => file.name.toLowerCase() === 'accounts.sqlite') || null;
}

export function labelFromWebkitFile(file) {
  const rel = String(file.webkitRelativePath || file.name || '').replace(/\\/g, '/');
  const parts = rel.split('/').filter(Boolean);
  if (parts.length >= 4 && parts.slice(-3).join('/').toLowerCase() === 'db/data/accounts.sqlite') {
    return parts.slice(0, -3).join('/');
  }
  if (parts.length > 1) {
    return parts.slice(0, -1).join('/');
  }
  return parts[0] || 'SD Farm';
}

export async function importAccountsFile(file, label) {
  const form = new FormData();
  form.append('file', file, 'accounts.sqlite');
  form.append('sdFarmRoot', label);
  const res = await fetch('/api/sd-farm/import', { method: 'POST', body: form });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Import failed');
  return data;
}

export function supportsDirectoryPicker() {
  return typeof window !== 'undefined' && typeof window.showDirectoryPicker === 'function';
}
