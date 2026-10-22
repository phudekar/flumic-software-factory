"""Validate published project-path links, anchors, assets and source coverage."""
import ast
import json
import struct
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

ROOT=Path(__file__).resolve().parents[2]
DIST=ROOT/'website/dist'
BASE='/flumic-software-factory/'

class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.ids=set(); self.links=[]; self.images=[]; self.meta={}
        self.feed(text)
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if 'id' in attrs: self.ids.add(attrs['id'])
        if tag in {'a','link'} and attrs.get('href'): self.links.append(attrs['href'])
        if tag in {'img','script'} and attrs.get('src'): self.links.append(attrs['src'])
        if tag=='img': self.images.append(attrs)
        if tag=='meta': self.meta[attrs.get('property') or attrs.get('name')]=attrs.get('content')

pages={p:Page(p.read_text()) for p in DIST.rglob('*.html')}
errors=[]
for path,page in pages.items():
    rel=path.relative_to(DIST).as_posix()
    url='https://example.test'+BASE+rel.removesuffix('index.html')
    for href in page.links:
        target=urlsplit(urljoin(url,href))
        if target.netloc!='example.test': continue
        if not target.path.startswith(BASE):
            errors.append(f'{rel}: path outside project base: {href}'); continue
        local=DIST/unquote(target.path[len(BASE):])
        if local.is_dir(): local=local/'index.html'
        if not local.exists(): errors.append(f'{rel}: missing target {href}'); continue
        if target.fragment and local in pages and unquote(target.fragment) not in pages[local].ids:
            errors.append(f'{rel}: missing anchor {href}')
    for key in ('og:title','og:description','og:image','og:image:alt','twitter:card','twitter:image'):
        if not page.meta.get(key): errors.append(f'{rel}: missing social metadata {key}')
    for key in ('og:image','twitter:image'):
        target=urlsplit(page.meta.get(key) or '')
        if target.scheme!='https' or target.netloc!='phudekar.github.io' or not target.path.startswith(BASE):
            errors.append(f'{rel}: invalid absolute social image URL for {key}')
        elif not (DIST/unquote(target.path[len(BASE):])).is_file():
            errors.append(f'{rel}: social image asset missing for {key}')
    for img in page.images:
        if not img.get('alt') and img.get('aria-hidden') != 'true': errors.append(f'{rel}: image missing alt text')
if (DIST/'favicon.svg').read_bytes() != (ROOT/'control-plane/frontend/src/assets/flumic.svg').read_bytes():
    errors.append('Website favicon differs from canonical application icon')
for asset,size in [('social/flumic-ai-v1.png',(1200,630)),('favicon.png',(192,192))]:
    image=DIST/asset
    if not image.is_file() or image.read_bytes()[:8]!=b'\x89PNG\r\n\x1a\n':
        errors.append(f'Raster sharing asset missing or not PNG: {asset}')
    elif struct.unpack('>II',image.read_bytes()[16:24])!=size:
        errors.append(f'Unexpected sharing image dimensions: {asset}')
coverage=json.loads((DIST/'coverage.json').read_text())
actual=set()
for name in ('main','worker_configs','workflow_controls'):
    tree=ast.parse((ROOT/f'control-plane/backend/app/{name}.py').read_text())
    for node in ast.walk(tree):
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                if isinstance(d,ast.Call) and isinstance(d.func,ast.Attribute) and d.func.attr in {'get','post','put','delete','patch'} and d.args and isinstance(d.args[0],ast.Constant) and str(d.args[0].value).startswith('/api/'):
                    actual.add(f'{d.func.attr.upper()} {d.args[0].value}')
if actual!=set(coverage['api_routes']): errors.append('Generated API route coverage does not match backend')
settings_tree=ast.parse((ROOT/'worker/factory/settings.py').read_text())
settings=next(n for n in settings_tree.body if isinstance(n,ast.ClassDef) and n.name=='Settings')
fields={n.target.id for n in settings.body if isinstance(n,ast.AnnAssign) and isinstance(n.target,ast.Name)}
if fields!=set(coverage['settings_fields']): errors.append('Generated configuration coverage does not match Settings')
# The narrative reference must explain every source-declared configuration field.
worker_guide=(ROOT/'docs/configuration-workers.md').read_text()
for name in fields:
    if f'### `{name}`' not in worker_guide:
        errors.append(f'Worker setting lacks detailed documentation: {name}')
workflow_guide=(ROOT/'docs/configuration-workflows.md').read_text()
configuration_tree=ast.parse((ROOT/'worker/factory/configuration.py').read_text())
for model in configuration_tree.body:
    if isinstance(model,ast.ClassDef) and model.name in {'TaskSettings','Labels'}:
        for field in model.body:
            if isinstance(field,ast.AnnAssign) and isinstance(field.target,ast.Name):
                name=('labels.' if model.name=='Labels' else '')+field.target.id
                if f'### `{name}`' not in workflow_guide:
                    errors.append(f'Workflow setting lacks detailed documentation: {name}')
control_guide=(ROOT/'docs/configuration-control-plane.md').read_text()
control_tree=ast.parse((ROOT/'control-plane/backend/app/settings.py').read_text())
control_settings=next(n for n in control_tree.body if isinstance(n,ast.ClassDef) and n.name=='AppSettings')
for field in control_settings.body:
    if isinstance(field,ast.AnnAssign) and isinstance(field.target,ast.Name):
        if f'`{field.target.id}`' not in control_guide:
            errors.append(f'Control Plane setting lacks detailed documentation: {field.target.id}')
for source,route in coverage['guides'].items():
    if not (DIST/route/'index.html').exists(): errors.append(f'Canonical guide not published: {source}')
for expected in ['workflows/stages','operations/scaling','workflows/models-prompts','observability/stage-accounting','reference/api','reference/configuration']:
    if not (DIST/expected/'index.html').exists(): errors.append(f'Required capability missing: {expected}')
if not list((DIST/'pagefind').glob('pagefind*.js')): errors.append('Search index missing')
for forbidden in ['.env','factory.sqlite3','login.json']:
    if list(DIST.rglob(forbidden)): errors.append(f'Private runtime file published: {forbidden}')
if errors:
    print('\n'.join(errors),file=sys.stderr);sys.exit(1)
print(f'Validated {len(pages)} HTML pages, local links/anchors/assets, search index, {len(actual)} API routes and {len(coverage["guides"])} canonical guides.')
