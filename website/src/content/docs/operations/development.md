---
title: Development and documentation updates
---

## Run application checks

From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r worker/requirements.txt -r control-plane/backend/requirements.lock
.venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=worker:control-plane/backend .venv/bin/python -m unittest discover -s control-plane/backend/tests -q
```

For the control-plane frontend:

```sh
cd control-plane/frontend
npm ci
npm test
npm run build
```

The existing test workflow runs these suites and the frontend build. Backend tests use SQLite unless explicitly configured with a dedicated PostgreSQL test URL. Do not run test fixtures against the normal factory database or start real workers/provider calls to validate a documentation change.

## Update this website

The site is a separate static package in the same repository. Use Node.js 24 and Python 3. It does not require a running application, Hatchet, Docker, or model credentials.

```sh
cd website
npm ci
npm run dev
```

Open the printed URL with `/flumic-software-factory/`. For the production artifact and search index:

```sh
npm run build
npm run check
npm run preview
```

Edit focused guides under `website/src/content/docs/`. Complete handbooks come directly from the repository guides at build time; edit those original Markdown files. API and configuration references are generated from Python AST without importing the application. Generated pages and copied images are ignored by Git.

Search is generated for the production build; test it with `preview`. Changes to canonical guides during a dev session require `npm run prepare-content` or a dev-server restart.

## Publishing

In repository **Settings → Pages**, choose **GitHub Actions** as the source. The **Documentation website** workflow builds and validates pull requests. Only pushes to `main` and manual runs on `main` can deploy. Check the deployment job and its returned URL before treating a site update as published.

The site uses the repository prefix `/flumic-software-factory/`; deployment and local preview must retain it. Changing the repository name requires updating site configuration and content/link scripts together.

[Website source and contribution details](https://github.com/phudekar/flumic-software-factory/blob/main/website/README.md) · [Architecture and extension points](/flumic-software-factory/handbook/architecture/)
