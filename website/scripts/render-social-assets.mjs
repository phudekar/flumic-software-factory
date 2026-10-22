// Raster previews for link crawlers, rendered from the application's canonical icon.
import { readFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import sharp from 'sharp';

const site = new URL('../', import.meta.url);
const publicDir = new URL('public/', site);
const logo = await readFile(new URL('../control-plane/frontend/src/assets/flumic.svg', site));
await mkdir(new URL('social/', publicDir), { recursive: true });
await sharp(logo).resize(192, 192).png().toFile(fileURLToPath(new URL('favicon.png', publicDir)));
const mark = logo.toString().replace('<svg ', '<svg x="74" y="58" width="76" height="76" ');
const steps = ['Issue', 'Plan', 'Implement', 'Review', 'Merge'];
const pipeline = steps.map((label, i) => {
  const x = 74 + i * 214;
  return `<rect x="${x}" y="457" width="192" height="72" rx="12" fill="#111e29" stroke="#2e4a49"/>
    <text x="${x + 96}" y="501" text-anchor="middle" fill="#dbe6ee" font-size="23">${label}</text>` +
    (i < 4 ? `<path d="M${x + 197} 493h12m-4-4 4 4-4 4" fill="none" stroke="#82e4bd" stroke-width="2"/>` : '');
}).join('');
const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
  <rect width="1200" height="630" fill="#0b1018"/>
  <path d="M0 0h1200v10H0Z" fill="#82e4bd"/>
  <circle cx="1145" cy="70" r="220" fill="#11342d" opacity=".45"/>
  <circle cx="1145" cy="70" r="160" fill="none" stroke="#285348" opacity=".6"/>
  <g font-family="DejaVu Sans, Arial, sans-serif">
    ${mark}
    <text x="168" y="111" fill="#edf4f8" font-size="42" font-weight="700">Flumic AI</text>
    <text x="76" y="220" fill="#edf4f8" font-size="60" font-weight="700">AI development.</text>
    <text x="76" y="294" fill="#82e4bd" font-size="60" font-weight="700">Under your control.</text>
    <text x="78" y="356" fill="#b0c1cc" font-size="25">Configurable workflows. Your models. Multiple workers.</text>
    <text x="78" y="394" fill="#b0c1cc" font-size="25">Trace every stage, token and known cost estimate.</text>
    ${pipeline}
    <text x="78" y="583" fill="#8299aa" font-size="20">Developer documentation · GitHub issues to reviewed pull requests</text>
  </g>
</svg>`;
await sharp(Buffer.from(svg)).png().toFile(fileURLToPath(new URL('social/flumic-ai-v1.png', publicDir)));
console.log('Rendered 1200×630 social banner and PNG icon from the canonical Folded F.');
