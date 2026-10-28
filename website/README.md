# Flumic AI website

Static Astro/Starlight product site and technical documentation for GitHub Pages.
The factory application does not need to run to build or browse this site.

## Local development

Use Node.js 24 (minimum 22.23.4), npm, and Python 3. No Python third-party packages
or provider credentials are needed for the website.

```sh
cd website
npm ci
npm run dev
```

Open the printed URL with `/flumic-software-factory/`. To test the deployed artifact:

```sh
npm run build
npm run check
npm run preview
```

## Content ownership

The canonical Flumic AI icon is `control-plane/frontend/src/assets/flumic.svg`.
Application logos and its bundled favicon use it directly. Starlight imports the
same SVG; the content preparation step copies it as the website favicon and
landing-page logo. Edit only that canonical source when changing the icon.

- `src/pages/index.astro`: custom product landing page.
- `src/content/docs/`: maintained tutorial and focused task guides.
- `scripts/prepare_content.py`: explicit allowlist of canonical repository guides,
  relative-link rewriting, images, Python AST route/configuration inventories.
- `src/content/docs/handbook/`, `reference/`, `architecture/decisions/` and `start/quick-start.md`: generated
  and ignored. Edit the source guides or generator, not these outputs.
- `public/images/` and `coverage.json`: generated from tracked screenshots and
  source coverage; do not copy live state or environment files into public assets.
- `scripts/check_site.py`: checks built links, anchors, assets, project prefix,
  API inventory coverage, canonical guides and search output.

README Quick start remains the canonical first-run guide. Source handbooks and
accepted ADRs in `docs/adr/` are built directly from their repository Markdown.
Add or update ADRs there; their index, pages and sidebar entries are generated.
Internal plans and historical migration proposals are not part of website navigation/search. Additional authored
pages explain specific UI tasks and link to the complete handbooks/reference.

After editing a canonical source during `dev`, rerun `npm run prepare-content`
(or restart `npm run dev`) to refresh its generated website page.

## GitHub Pages deployment

The expected URL is https://phudekar.github.io/flumic-software-factory/.
The project base is configured in `astro.config.mjs` and the content/link scripts;
update them together if the repository path changes.

1. In GitHub repository **Settings → Pages → Build and deployment**, select
   **GitHub Actions** as the source. The workflow does not alter repository settings.
2. Merge/push the site changes to `main`, or dispatch **Documentation website**.
3. Confirm the deployment job and its returned URL in Actions/Environments.

Pull requests only build and validate. Trusted main pushes and manual runs on
main can deploy through `.github/workflows/pages.yml`. Only the deployment job
has `pages: write` and `id-token: write`. No application secrets are used.

## Coverage map

| Source / product surface | Website destination |
| --- | --- |
| README first-run instructions | Start here → Quick start |
| Every top-level current operator guide | Complete handbook |
| All API decorators across main, profiles and workflow controls | Source-derived reference → API endpoint reference |
| Worker settings, task/label schemas, exact validation and env reads | Source-derived reference → Configuration fields and validation |
| All built-in task definitions | Source-derived reference → Task defaults and stage inventory |
| Overview and navigation | Control plane → Navigate the control plane |
| Worker list/detail/build/startup/lifecycle | Control plane → Manage workers and images |
| Workflows and action receipts | Workflows → Trigger, pause and resume |
| Runs/stages/attempts/logs/model history | Control plane → Investigate runs, stages and logs |
| Usage page and per-stage accounting | Observability → Trace each stage, token and cost |
| Profile/factory editors and worker prompts | Control plane → Profiles, prompts and factory settings |
| SigNoz, OTLP, pricing, resources and smoke tests | Observability setup + complete observability handbook |
| Security, storage, upgrades and worker capacity | Operations + architecture handbook |

`coverage.json` in the build contains the guide, route and settings inventory for
inspection. `npm run check` verifies source-route parity and required topics.

## Link previews and sharing assets

Every built page includes an absolute Open Graph PNG URL and large-image card
metadata. The homepage adds its own title/description; docs retain their page
specific metadata. `scripts/render-social-assets.mjs` renders a 1200×630 banner
and 192×192 PNG favicon from the canonical frontend Folded F icon using Sharp.
`npm run prepare-content` regenerates both, so fresh CI checkouts need no local
asset files. `npm run check` verifies social tags, image targets and dimensions.

Sharing assets are public static files; no browser login or JavaScript is required
for crawlers. Slack controls its own preview layout and caching. Existing messages
may retain old previews after a deployment. Repost the link after publishing; a
new query string such as `?share=flumic-v1` can help test a newly fetched URL.
See [Slack's crawler documentation](https://api.slack.com/robots).
