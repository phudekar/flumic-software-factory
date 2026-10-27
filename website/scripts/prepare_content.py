"""Build public reference material from explicit, non-secret repository sources.

Uses only Python's standard library; does not import the application or load env.
"""
import ast
from api_routes import route_nodes
import json
import re
import shutil
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / 'website'
CONTENT = SITE / 'src/content/docs'
BASE = '/flumic-software-factory/'
GITHUB = 'https://github.com/phudekar/flumic-software-factory/blob/main/'
GUIDES = {
    'docs/control-plane.md': 'Control-plane handbook',
    'docs/worker-configuration.md': 'Worker configuration handbook',
    'docs/configuration-workers.md': 'Worker settings: effects and examples',
    'docs/configuration-workflows.md': 'Workflow settings: effects and examples',
    'docs/configuration-control-plane.md': 'Control Plane settings: effects and examples',
    'docs/worker-image-builds.md': 'Worker image builds',
    'docs/project-runtime-api.md': 'Project workflow runtime API',
    'docs/kubernetes.md': 'Kubernetes runtime configuration and operations',
    'docs/gitlab.md': 'GitLab.com setup and workflow behavior',
    'docs/project-runtime-operations.md': 'Project runtime rollout and qualification',
    'docs/docker-action-jobs.md': 'One-time Docker actions',
    'docs/control-plane-workflows.md': 'Workflow control architecture',
    'docs/project-observability.md': 'Project history, traces and AI accounting',
    'docs/observability.md': 'Observability handbook',
    'docs/issue-lifecycle.md': 'Issue lifecycle and webhooks',
    'docs/architecture.md': 'Architecture and extensions',
    'docs/workflow-development.md': 'Develop workflows and link stages',
    'docs/telemetry-error-audit.md': 'Error-path audit',
    'config/worker-profiles/README.md': 'Bootstrap profile handbook',
}


def route(path):
    return 'handbook/' + ('bootstrap-profiles' if path.startswith('config/') else Path(path).stem) + '/'


def rewrite_links(text, source):
    def replace(match):
        href = match.group(1)
        if href.startswith(('http:', 'https:', '#', 'mailto:')):
            return match.group(0)
        file, _, fragment = href.partition('#')
        target = (ROOT / source).parent.joinpath(file).resolve()
        try:
            relative = target.relative_to(ROOT).as_posix()
        except ValueError:
            raise ValueError(f'Link outside repository: {source}: {href}')
        if not target.exists():
            raise ValueError(f'Missing link: {source}: {href}')
        if relative in GUIDES:
            url = BASE + route(relative)
        elif relative == 'README.md':
            url = BASE + 'start/quick-start/'
            # README fragments outside quick start go to the original source.
            if fragment and fragment not in {'quick-start', '1-prepare-your-environment', '2-start-the-control-plane', '3-start-a-simple-worker-and-a-developer-worker', '4-process-your-first-issue'}:
                url = GITHUB + relative
        elif relative.startswith('docs/images/'):
            url = BASE + 'images/' + target.name
        else:
            url = GITHUB + quote(relative)
        return f']({url}' + (f'#{fragment}' if fragment else '') + ')'
    return re.sub(r'\]\(([^)\s]+)\)', replace, text)


def write(slug, title, body, description=''):
    path = CONTENT / (slug + '.md')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('---\ntitle: ' + json.dumps(title) + '\ndescription: ' + json.dumps(description or title) + '\n---\n\n' + body.strip() + '\n')


def code(node):
    return ast.unparse(node) if node is not None else 'required'


def inline(value):
    return '`' + str(value).replace('|', '&#124;').replace('\n', ' ') + '`'


def source_link(path, node):
    return f'[Source]({GITHUB}{path}#L{node.lineno})'


def model_fields(node):
    fields = []
    for item in node.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            fields.append((item.target.id, code(item.annotation), code(item.value)))
    return fields


def table(fields):
    return '| Field | Type | Default / validation |\n| --- | --- | --- |\n' + '\n'.join('| ' + ' | '.join(map(inline, item)) + ' |' for item in fields) + '\n'


for source, title in GUIDES.items():
    body = (ROOT / source).read_text()
    body = re.sub(r'^# .+\n', '', body, count=1)
    body = rewrite_links(body, source)
    write(route(source).rstrip('/'), title, f'Canonical guide: [edit in GitHub]({GITHUB}{source}). This page is prepared from the repository guide at build time.\n\n' + body)

readme = (ROOT / 'README.md').read_text()
quick = readme.split('## Quick start\n', 1)[1].split('## Configuration\n', 1)[0]
write('start/quick-start', 'Quick start', rewrite_links(quick, 'README.md'))
# Generated directories are absent from fresh checkouts.
(SITE / 'public').mkdir(exist_ok=True, parents=True)
# Canonical icon lives with the app; docs and favicons consume that same SVG.
shutil.copy2(ROOT / 'control-plane/frontend/src/assets/flumic.svg', SITE / 'public/favicon.svg')

images = SITE / 'public/images'
images.mkdir(exist_ok=True, parents=True)
for image in (ROOT / 'docs/images').iterdir():
    if image.suffix.lower() not in {'.png', '.svg'}:
        continue
    shutil.copy2(image, images / image.name)

routes = []
models = {}
api_paths = [path.relative_to(ROOT).as_posix() for path in sorted((ROOT / 'control-plane/backend/app').glob('*.py'))]
for path in api_paths:
    tree = ast.parse((ROOT / path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(code(base) == 'BaseModel' or code(base) in models for base in node.bases):
            models[node.name] = (path, node)
    for method, url, node, decorator in route_nodes(tree):
        routes.append((method, url, path, node, decorator))

api = ['Every decorated API route in the backend is listed below, including compatibility aliases. '
       'Generated directly from handlers without starting the application. Read [authentication and errors](' + BASE + 'api/authentication/) '
       'and [request recipes](' + BASE + 'api/recipes/) before making requests. '
       'The [response contracts](' + BASE + 'api/contracts/) explain resources and asynchronous receipts.\n',
       f'**{len(routes)} method/path combinations** across backend route modules. '
       'Parameter defaults retain their Python declaration so query bounds and strict types are visible. '
       'Request/response shape details that depend on adapters are linked to the implementation.\n']
for method, url, path, node, decorator in routes:
    api.append(f'## {method} {url}\n\nHandler: `{node.name}` · {source_link(path, node)}\n')
    if url in {'/api/v1/worker-bootstrap', '/api/v1/ingest'}:
        auth = 'Worker-scoped bearer token and `X-Worker-Id` headers; no administrator session.'
    elif url.startswith('/api/v1/hooks/gitlab/'):
        auth = 'Binding-scoped `X-Gitlab-Token`; no administrator session or CSRF. See the GitLab setup guide for token rotation, payload limits and delivery IDs.'
    elif url == '/api/v1/health':
        auth = 'Public health check; verifies database access.'
    elif url == '/api/v1/auth/login':
        auth = 'Administrator password; creates a session. Rate limited.'
    else:
        auth = 'Administrator session cookie' + (' and `X-CSRF-Token` for this mutation.' if method != 'GET' else '.')
    api.append('**Authentication:** ' + auth + '\n')
    args = node.args.args
    defaults = [None] * (len(args) - len(node.args.defaults)) + node.args.defaults
    params = [(arg.arg, code(arg.annotation), code(default)) for arg, default in zip(args, defaults) if arg.arg not in {'request', 'response'}]
    if params:
        api.append(table(params))
    status = next((code(kw.value) for kw in decorator.keywords if kw.arg == 'status_code'), '200')
    api.append(f'**Success status:** `{status}`. ' + ('This is an operation receipt, not confirmation that the runtime action finished.' if node.name in {'create_worker', 'worker_action'} else '') + '\n')
    returned = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Return) and isinstance(child.value, ast.Dict):
            returned.update(k.value for k in child.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str))
    if returned:
        api.append('**Top-level response keys from handler branches:** ' + ', '.join(inline(v) for v in sorted(returned)) + '. See the linked source for branch conditions and nested values.\n')
    errors = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and code(child.func) == 'HTTPException' and len(child.args) >= 2:
            errors.append(f'- `{code(child.args[0])}`: {code(child.args[1])}')
    if errors:
        api.append('**Handler errors** (in addition to common middleware/validation failures):\n\n' + '\n'.join(dict.fromkeys(errors)) + '\n')
    body_arg = next((arg for arg in args if arg.arg == 'body'), None)
    if body_arg and code(body_arg.annotation) in models:
        api.append('**Request body:** see `' + code(body_arg.annotation) + '` under [request models](#request-models).\n')
api.append('## Request models\n\nTypes, defaults and field limits below are generated from Pydantic declarations. Nested configuration dictionaries also undergo application validation.\n')
for name, (path, node) in models.items():
    parents = [code(base) for base in node.bases if code(base) in models]
    inherited = ('Inherits ' + ', '.join(f'[{parent}](#{parent.lower()})' for parent in parents) + '.\n\n') if parents else ''
    api.append(f'### {name}\n\n{source_link(path, node)}\n\n' + inherited + table(model_fields(node)))
write('reference/api', 'API endpoint reference', '\n'.join(api))

settings_path = 'worker/factory/settings.py'
settings_tree = ast.parse((ROOT / settings_path).read_text())
settings = next(n for n in settings_tree.body if isinstance(n, ast.ClassDef) and n.name == 'Settings')
configuration_path = 'worker/factory/configuration.py'
config_tree = ast.parse((ROOT / configuration_path).read_text())
config_models = [n for n in config_tree.body if isinstance(n, ast.ClassDef)]
body = ['Read the detailed guides for [worker execution settings](' + BASE + 'handbook/configuration-workers/), [workflow/task/label settings](' + BASE + 'handbook/configuration-workflows/) and [Control Plane/environment settings](' + BASE + 'handbook/configuration-control-plane/) for effects, examples and adoption timing.\n',
        'Types and code defaults below come directly from the repository. Checked-in YAML and saved profiles can override code defaults; consult the [configuration handbook](' + BASE + 'handbook/worker-configuration/) for precedence. '
        'The `control_revision` field is runtime metadata, not an operator override.\n',
        '## Worker settings\n\n' + source_link(settings_path, settings) + '\n\n' + table(model_fields(settings))]
for model in config_models:
    body.append('## ' + model.name + '\n\n' + source_link(configuration_path, model) + '\n\n' + table(model_fields(model)))
body.append('## Validation rules\n\nThe following validation is reproduced from `Settings.__post_init__` to preserve exact bounds, cross-field checks and allowed values. The YAML loader additionally rejects unknown fields and validates task settings using the declarations above.\n')
validator = next(n for n in settings.body if isinstance(n, ast.FunctionDef) and n.name == '__post_init__')
body.append('```python\n' + ast.unparse(validator) + '\n```')
body.append('## Container environment configuration\n\nThese are the environment reads in application configuration entry points. They include compatibility-only APIs: `Settings.load()` uses YAML, while `Settings.from_env()` remains a compatibility constructor. Do not assume legacy non-secret variables override deployed YAML.\n')
for path in ['control-plane/backend/app/settings.py', settings_path]:
    tree = ast.parse((ROOT/path).read_text())
    entries = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'get' and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            receiver = code(node.func.value)
            if receiver in {'env', 'os.environ'}:
                entries.add((node.args[0].value, code(node.args[1]) if len(node.args) > 1 else 'None'))
        if isinstance(node, ast.Subscript) and code(node.value) == 'os.environ' and isinstance(node.slice, ast.Constant):
            entries.add((node.slice.value, 'required'))
    body.append('### ' + path + '\n\n| Variable | Code fallback |\n| --- | --- |\n' + '\n'.join(f'| {inline(k)} | {inline(v)} |' for k,v in sorted(entries)))
write('reference/configuration', 'Configuration fields and validation', '\n\n'.join(body))

# Source links ensure task names/engine definitions remain discoverable without
# importing Hatchet or evaluating worker registration.
task_path = 'worker/factory/task_defaults.py'
task_tree = ast.parse((ROOT/task_path).read_text())
task_source = (ROOT/task_path).read_text()
task_defaults = ast.literal_eval(next(n.value for n in task_tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'TASK_DEFAULTS' for t in n.targets)))
orchestration_path = 'worker/factory/adapters/orchestration/hatchet.py'
engine_tree = ast.parse((ROOT / orchestration_path).read_text())
registrations = {}
for node in ast.walk(engine_tree):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute) or decorator.func.attr not in {'task', 'durable_task'}:
            continue
        registrations[node.name] = (node, decorator)
tasks = ['These are the built-in stages and standalone/durable tasks. Defaults come from code; checked-in YAML and saved configuration may override them. Use [stage management](' + BASE + 'workflows/stages/) for adoption and precedence.\n']
for name, defaults in task_defaults.items():
    tasks.append('## ' + name + '\n')
    if name in registrations:
        node, decorator = registrations[name]
        options = {k.arg: code(k.value) for k in decorator.keywords if k.arg}
        tasks.append('Engine name: ' + inline(options.get('name', name)) + ' · ' + source_link(orchestration_path, node) + '\n')
        tasks.append('Input: ' + inline(next((code(a.annotation) for a in node.args.args if a.arg == 'input'), 'none')) + '; output: ' + inline(code(node.returns)) + '.\n')
        for key in ('parents', 'input_validator', 'on_events', 'on_crons'):
            if key in options:
                tasks.append('**' + key.replace('_', ' ').title() + ':** ' + inline(options[key]) + '\n')
        if decorator.func.attr == 'durable_task':
            tasks.append('Durable wait: sleeps between checks without holding active coding work.\n')
    tasks.append('```json\n' + json.dumps(defaults, indent=2) + '\n```')
write('reference/tasks', 'Task defaults and stage inventory', '\n'.join(tasks))


coverage = {'guides': {source: route(source) for source in GUIDES}, 'api_routes': [f'{m} {u}' for m,u,*_ in routes], 'settings_fields': [n for n,*_ in model_fields(settings)], 'ui_pages': ['overview', 'workers', 'worker-details', 'workflows', 'workflow-runs', 'logs', 'model-history', 'usage', 'configuration', 'worker-prompts']}
(SITE/'public/coverage.json').write_text(json.dumps(coverage, indent=2)+'\n')
print(f'Prepared {len(GUIDES)} canonical guides, {len(routes)} API routes, {len(coverage["settings_fields"])} settings and task defaults.')
