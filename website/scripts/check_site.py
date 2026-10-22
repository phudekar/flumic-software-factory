"""Validate published project-path links, anchors, assets and source coverage."""
import ast
import json
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

ROOT=Path(__file__).resolve().parents[2]
DIST=ROOT/'website/dist'
BASE='/software-factory/'

class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.ids=set(); self.links=[]; self.images=[]
        self.feed(text)
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if 'id' in attrs: self.ids.add(attrs['id'])
        if tag in {'a','link'} and attrs.get('href'): self.links.append(attrs['href'])
        if tag in {'img','script'} and attrs.get('src'): self.links.append(attrs['src'])
        if tag=='img': self.images.append(attrs)

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
    for img in page.images:
        if not img.get('alt') and img.get('aria-hidden') != 'true': errors.append(f'{rel}: image missing alt text')
if (DIST/'favicon.svg').read_bytes() != (ROOT/'control-plane/frontend/src/assets/flumic.svg').read_bytes():
    errors.append('Website favicon differs from canonical application icon')
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
