import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import { socialHead } from './src/social.mjs';

export default defineConfig({
  site: 'https://phudekar.github.io',
  base: '/flumic-software-factory',
  trailingSlash: 'always',
  integrations: [starlight({
    title: 'Flumic AI',
    favicon: '/favicon.png',
    head: socialHead,
    logo: { src: '../control-plane/frontend/src/assets/flumic.svg', alt: 'Flumic AI icon' },
    description: 'Run, configure, and observe AI development workflows from GitHub issue to reviewed pull request.',
    social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/phudekar/flumic-software-factory' }],
    customCss: ['./src/styles/docs.css'],
    sidebar: [
      { label: 'Start here', items: ['start/introduction', 'start/quick-start', 'start/concepts'] },
      { label: 'Workflows & stages', items: [{ autogenerate: { directory: 'workflows' } }] },
      { label: 'Control plane', items: [{ autogenerate: { directory: 'control-plane' } }] },
      { label: 'Configuration', items: [{ autogenerate: { directory: 'configuration' } }] },
      { label: 'Backend API', items: [{ autogenerate: { directory: 'api' } }] },
      { label: 'Observability', items: [{ autogenerate: { directory: 'observability' } }] },
      { label: 'Operations', items: [{ autogenerate: { directory: 'operations' } }] },
      { label: 'Complete handbook', collapsed: true, items: [{ autogenerate: { directory: 'handbook' } }] },
      { label: 'Source-derived reference', collapsed: true, items: [{ autogenerate: { directory: 'reference' } }] },
    ],
  })],
});
