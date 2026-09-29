// QR code as inline SVG (vendored `uqr`, MIT). <QrCode text="http://192.168.1.5:8000/?token=…" size={200} />
import { html } from "../lib/html.js";
import { encode } from "uqr";

export function QrCode({ text, size = 200, label = "QR-код" }) {
  if (!text) return null;
  let data;
  try {
    data = encode(text, { ecc: "M", border: 2 }).data;
  } catch {
    return null;
  }
  const n = data.length;
  let d = "";
  for (let y = 0; y < n; y++) {
    for (let x = 0; x < n; x++) if (data[y][x]) d += `M${x} ${y}h1v1h-1z`;
  }
  return html`<svg class="qr" width=${size} height=${size} viewBox=${`0 0 ${n} ${n}`} role="img" aria-label=${label} shape-rendering="crispEdges">
    <rect width=${n} height=${n} fill="#fff" />
    <path d=${d} fill="#0b0d11" />
  </svg>`;
}
