/**
 * Copy text to the clipboard. On plain HTTP (e.g. VPS IP), `navigator.clipboard`
 * is often undefined — use a hidden textarea + execCommand fallback.
 */
export async function copyToClipboard(text) {
  const value = String(text ?? '');
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const ta = document.createElement('textarea');
  ta.value = value;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  ta.style.top = '0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  ta.setSelectionRange(0, value.length);
  try {
    const ok = document.execCommand('copy');
    if (!ok) {
      throw new Error('Copy command was blocked');
    }
  } finally {
    document.body.removeChild(ta);
  }
}
