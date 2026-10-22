// Absolute public URLs allow Slack and other crawlers to fetch images directly.
const root = 'https://phudekar.github.io/flumic-software-factory/';
export const socialHead = [
  { tag: 'meta', attrs: { property: 'og:image', content: root + 'social/flumic-ai-v1.png' } },
  { tag: 'meta', attrs: { property: 'og:image:secure_url', content: root + 'social/flumic-ai-v1.png' } },
  { tag: 'meta', attrs: { property: 'og:image:type', content: 'image/png' } },
  { tag: 'meta', attrs: { property: 'og:image:width', content: '1200' } },
  { tag: 'meta', attrs: { property: 'og:image:height', content: '630' } },
  { tag: 'meta', attrs: { property: 'og:image:alt', content: 'Flumic AI: AI development under your control. Issue, plan, implement, review and merge.' } },
  { tag: 'meta', attrs: { name: 'twitter:card', content: 'summary_large_image' } },
  { tag: 'meta', attrs: { name: 'twitter:image', content: root + 'social/flumic-ai-v1.png' } },
  { tag: 'meta', attrs: { name: 'twitter:image:alt', content: 'Flumic AI workflow from issue to reviewed pull request' } },
];
