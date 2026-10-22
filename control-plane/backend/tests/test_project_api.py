"""Project-qualified APIs must reject foreign references and preserve legacy aliases."""
import copy
import time
import unittest
import uuid
import test_api as fixtures
from sqlalchemy import select
from app.database import DEFAULT_PROJECT_ID, Revision, Event, Conversation


class ProjectAPITests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown

    def project(self, slug):
        response = self.client.post('/api/v1/projects', json={'slug':slug, 'name':slug})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def test_two_projects_keep_repositories_and_profiles_separate(self):
        a, b = self.project('alpha'), self.project('beta')
        for project in (a, b):
            for alias in ('backend', 'frontend'):
                response = self.client.post(f'/api/v1/projects/{project}/repositories', json={'alias':alias,'owner':'example','name':alias})
                self.assertEqual(response.status_code,201,response.text)
        repos = self.client.get(f'/api/v1/projects/{a}/repositories').json()
        self.assertEqual(len(repos),2)
        foreign = self.client.get(f'/api/v1/projects/{b}/repositories/{repos[0]["id"]}')
        self.assertEqual(foreign.status_code,404)
        profiles = self.client.get(f'/api/v1/projects/{a}/worker-profiles').json()
        self.assertTrue(profiles)
        self.assertTrue(all(p['project_id']==a for p in profiles))
        self.assertTrue(all(p['project_id']==DEFAULT_PROJECT_ID for p in self.client.get('/api/v1/worker-profiles').json()))

    def test_foreign_factory_parent_and_profile_clones_are_rejected(self):
        a, b = self.project('alpha'), self.project('beta')
        factory_a = self.client.get(f'/api/v1/projects/{a}/factory-config').json()
        response = self.client.post(f'/api/v1/projects/{b}/factory-config', json={'parent_id':factory_a['id'], 'document':factory_a['document']})
        self.assertEqual(response.status_code,404,response.text)
        profile = self.client.get(f'/api/v1/projects/{a}/worker-profiles').json()[0]
        factory_b = self.client.get(f'/api/v1/projects/{b}/factory-config').json()
        body = {k:profile[k] for k in ('name','profile','document')}
        body.update(factory_revision_id=factory_b['id'],cloned_from=profile['id'])
        self.assertEqual(self.client.post(f'/api/v1/projects/{b}/worker-profiles',json=body).status_code,404)
        self.assertEqual(self.client.get('/api/v1/config/revisions/'+factory_a['id']).status_code,404)
        self.assertEqual(self.client.get('/api/v1/config/revisions/'+factory_a['id']+'/export').status_code,404)

    def test_foreign_event_and_conversation_are_not_visible(self):
        a, b = self.project('alpha'), self.project('beta')
        identifier = str(uuid.uuid4())
        with self.sessions.begin() as db:
            db.add(Event(id=identifier,project_id=a,kind='model.completed',worker_id='shared',occurred_at=time.time(),payload={'input_tokens':10}))
            from cryptography.fernet import Fernet
            encrypted = Fernet(self.settings.encryption_key.encode()).encrypt(b'{"text":"private"}').decode()
            db.add(Conversation(id=identifier,project_id=a,ciphertext=encrypted,expires_at=time.time()+100))
        self.assertEqual(len(self.client.get(f'/api/v1/projects/{a}/events').json()),1)
        self.assertEqual(self.client.get(f'/api/v1/projects/{b}/events').json(),[])
        self.assertEqual(self.client.get('/api/v1/events').json(),[])
        self.assertEqual(self.client.get(f'/api/v1/projects/{b}/model-calls/{identifier}/conversation').status_code,404)
        self.assertEqual(self.client.get('/api/v1/model-calls/'+identifier+'/conversation').status_code,404)
        self.assertEqual(self.client.get(f'/api/v1/projects/{a}/model-calls/{identifier}/conversation').json(),{'text':'private'})

    def test_default_alias_cannot_publish_against_foreign_revision(self):
        a = self.project('alpha')
        profile = self.client.get(f'/api/v1/projects/{a}/worker-profiles').json()[0]
        body={k:profile[k] for k in ('name','profile','document','factory_revision_id')}
        self.assertEqual(self.client.post('/api/v1/worker-profiles',json=body).status_code,404)

    def test_repository_updates_publish_revisions_and_reject_foreign_and_stale_parents(self):
        a,b=self.project('alpha'),self.project('beta')
        path=f'/api/v1/projects/{a}/repositories'
        first=self.client.post(path,json={'alias':'backend','owner':'example','name':'api'}).json()
        self.assertIsNotNone(first.get('revision_id'),'repository creation must publish a revision')
        body={'alias':'backend','owner':'example','name':'api2','parent_id':first['revision_id']}
        self.assertEqual(self.client.put(f'/api/v1/projects/{b}/repositories/{first["id"]}',json=body).status_code,404)
        updated=self.client.put(path+'/'+first['id'],json=body)
        self.assertEqual(updated.status_code,200,updated.text)
        self.assertNotEqual(updated.json()['revision_id'],first['revision_id'])
        self.assertEqual(self.client.put(path+'/'+first['id'],json=body).status_code,409)
        self.assertEqual(self.client.get(f'/api/v1/projects/{b}/config/revisions/{self.revision["id"]}/export').status_code,404)

    def test_effective_preview_merges_behavior_and_enforces_owner_and_limits(self):
        a,b=self.project('alpha'),self.project('beta')
        path=f'/api/v1/projects/{a}'
        created=self.client.post(path+'/repositories',json={'alias':'backend','owner':'alpha','name':'api','overrides':{'openai_model':'repo-model','api_timeout_seconds':45.0}})
        self.assertEqual(created.status_code,201,created.text)
        body={'repository':'backend','profile':'simple','workflow':{'openai_model':'workflow-model'},'stage':{'openai_model':'stage-model'}}
        response=self.client.post(path+'/config/effective',json=body)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['settings']['repository'],'alpha/api')
        self.assertEqual(response.json()['settings']['openai_model'],'stage-model')
        self.assertEqual(response.json()['settings']['api_timeout_seconds'],45.0)
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(self.client.post(f'/api/v1/projects/{b}/config/effective',json=body).status_code,404)
        body['stage']={'repository':'beta/api'}
        self.assertEqual(self.client.post(path+'/config/effective',json=body).status_code,422)
        body['stage']={'max_files_per_iteration':6}
        self.assertEqual(self.client.post(path+'/config/effective',json=body).status_code,422)
        body['stage']={'state_factory':'arbitrary:code'}
        self.assertEqual(self.client.post(path+'/config/effective',json=body).status_code,422)

    def test_scoped_revision_publish_and_export_use_project_history(self):
        a=self.project('alpha')
        path=f'/api/v1/projects/{a}/config/revisions'
        latest=self.client.get(path).json()[0]
        document=self.client.get(path+'/'+latest['id']).json()['document']
        document['defaults']['openai_model']='project-model'
        response=self.client.post(path,json={'parent_id':latest['id'],'document':document})
        self.assertEqual(response.status_code,201,response.text)
        self.assertEqual(response.json()['project_id'],a)
        self.assertIn('project-model',self.client.get(path+'/'+response.json()['id']+'/export').text)
        self.assertEqual(self.client.get('/api/v1/config/revisions').json()[0]['id'],self.revision['id'])

    def test_worker_endpoints_reject_foreign_profile_families(self):
        a=self.project('alpha')
        profile=self.client.get(f'/api/v1/projects/{a}/worker-profiles').json()[0]
        response=self.client.post('/api/v1/workers',json={'name':'bad-profile','worker_profile_id':profile['family_id']})
        self.assertEqual(response.status_code,404,response.text)

    def test_workflow_control_receipts_and_keys_are_project_scoped(self):
        from app.database import WorkflowControlOperation
        a,b=self.project('alpha'),self.project('beta')
        with self.sessions.begin() as db:
            for project in (a,b):
                db.add(WorkflowControlOperation(project_id=project,key='same-request',request_digest='digest',workflow_id=str(uuid.uuid4()),workflow_name='legacy',action='trigger',status='completed'))
        items=self.client.get(f'/api/v1/projects/{a}/workflow-controls')
        self.assertEqual(items.status_code,200,items.text)
        self.assertEqual(len(items.json()),1)
        self.assertEqual(items.json()[0]['project_id'],a)
        identifier=items.json()[0]['id']
        self.assertEqual(self.client.get(f'/api/v1/projects/{b}/workflow-controls/{identifier}').status_code,404)
        self.assertEqual(self.client.get('/api/v1/workflow-controls').json(),[])

    def test_stage_repository_selection_and_task_precedence_remain_project_scoped(self):
        a=self.project('alpha')
        path=f'/api/v1/projects/{a}'
        for alias in ('backend','frontend'):
            self.assertEqual(self.client.post(path+'/repositories',json={'alias':alias,'owner':'alpha','name':alias}).status_code,201)
        body={'repository':'backend','stage_repository':'frontend','workflow':{'tasks':{'triage_issue':{'model':'workflow-model'}}},'stage':{'tasks':{'triage_issue':{'model':'stage-model'}}}}
        response=self.client.post(path+'/config/effective',json=body)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['settings']['repository'],'alpha/frontend')
        self.assertEqual(response.json()['settings']['tasks']['triage_issue']['model'],'stage-model')
        body['stage_repository']='missing'
        self.assertEqual(self.client.post(path+'/config/effective',json=body).status_code,404)
        body['stage_repository']='frontend'
        body['stage']={'tasks':{'triage_issue':{'worker_type':'developer'}}}
        self.assertEqual(self.client.post(path+'/config/effective',json=body).status_code,422)

    def test_repository_validation_rolls_back_and_server_limits_cannot_be_overridden(self):
        a=self.project('alpha')
        path=f'/api/v1/projects/{a}'
        response=self.client.post(path+'/repositories',json={'alias':'bad','owner':'alpha','name':'bad','overrides':{'max_files_per_iteration':6}})
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.client.get(path+'/repositories').json(),[])
        repo=self.client.post(path+'/repositories',json={'alias':'backend','owner':'alpha','name':'api'}).json()
        from app.projects import effective_configuration,ProjectScope
        from app.services import Configurations
        from fastapi import HTTPException
        with self.sessions() as db:
            with self.assertRaises(ValueError):
                effective_configuration(db,ProjectScope(a),Configurations(fixtures.ROOT),'backend',stage={'api_timeout_seconds':31.0},limits={'api_timeout_seconds':30})
            result,_,_=effective_configuration(db,ProjectScope(a),Configurations(fixtures.ROOT),'backend',stage={'api_timeout_seconds':15.0},limits={'api_timeout_seconds':30})
            self.assertEqual(result.api_timeout_seconds,15.0)
        # A client cannot provide or increase deployment/pool policy in the body.
        self.assertEqual(self.client.post(path+'/config/effective',json={'repository':'backend','limits':{'api_timeout_seconds':9999}}).status_code,422)

    def test_foreign_legacy_workflow_control_history_is_hidden(self):
        from app.database import WorkflowControlOperation
        a=self.project('alpha')
        identifier=str(uuid.uuid4())
        with self.sessions.begin() as db:
            db.add(WorkflowControlOperation(project_id=a,key='foreign-control-key',request_digest='digest',workflow_id=identifier,workflow_name='legacy',action='trigger',status='completed'))
        self.workflow.definition.return_value={'name':'legacy'}
        self.assertEqual(self.client.get('/api/v1/workflows/'+identifier+'/actions').json(),[])

    @unittest.skipUnless(__import__('os').environ.get('CONTROL_TEST_DATABASE_URL','').startswith('postgresql'),'PostgreSQL concurrency fixture')
    def test_concurrent_repository_revision_updates_conflict(self):
        from concurrent.futures import ThreadPoolExecutor
        a=self.project('alpha')
        path=f'/api/v1/projects/{a}/repositories'
        first=self.client.post(path,json={'alias':'backend','owner':'alpha','name':'api'}).json()
        body={'alias':'backend','owner':'alpha','name':'updated','parent_id':first['revision_id']}
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(lambda _:self.client.put(path+'/'+first['id'],json=body).status_code,range(2)))
        self.assertEqual(sorted(statuses),[200,409])

    def test_openapi_documents_project_identity_in_every_scoped_path(self):
        paths=self.app.openapi()['paths']
        for path,methods in paths.items():
            if '/projects/{project_id}' not in path:
                continue
            for method,operation in methods.items():
                self.assertTrue(any(p['name']=='project_id' and p['in']=='path' and p['required'] for p in operation.get('parameters',[])),(method,path))

    def test_documented_routes_match_runtime_including_router_prefixes(self):
        import ast,importlib.util
        module_path=fixtures.ROOT/'website/scripts/api_routes.py'
        spec=importlib.util.spec_from_file_location('api_route_docs',module_path)
        helper=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        documented=set()
        for path in (fixtures.ROOT/'control-plane/backend/app').glob('*.py'):
            documented.update((method.lower(),url) for method,url,*_ in helper.route_nodes(ast.parse(path.read_text())))
        runtime={(method,path) for path,methods in self.app.openapi()['paths'].items() if path.startswith('/api/') for method in methods}
        self.assertEqual(documented,runtime)

    def test_effective_configuration_can_resolve_owned_pinned_revisions(self):
        a=self.project('alpha')
        path=f'/api/v1/projects/{a}'
        repo=self.client.post(path+'/repositories',json={'alias':'backend','owner':'alpha','name':'old','overrides':{'openai_model':'old-model'}}).json()
        factory=self.client.get(path+'/factory-config').json()
        response=self.client.put(path+'/repositories/'+repo['id'],json={'alias':'backend','owner':'alpha','name':'new','parent_id':repo['revision_id'],'overrides':{'openai_model':'new-model'}})
        self.assertEqual(response.status_code,200,response.text)
        from app.projects import effective_configuration,ProjectScope
        from app.services import Configurations
        with self.sessions() as db:
            resolved,_,_=effective_configuration(db,ProjectScope(a),Configurations(fixtures.ROOT),'backend',factory_revision_id=factory['id'],repository_revision_id=repo['revision_id'])
            self.assertEqual((resolved.repository,resolved.openai_model),('alpha/old','old-model'))
            current,_,_=effective_configuration(db,ProjectScope(a),Configurations(fixtures.ROOT),'backend')
            self.assertEqual((current.repository,current.openai_model),('alpha/new','new-model'))

    def test_ignored_foreign_references_are_rejected_before_validation_or_receipts(self):
        a=self.project('alpha')
        foreign=self.client.get(f'/api/v1/projects/{a}/worker-profiles').json()[0]
        own=self.client.get('/api/v1/worker-profiles').json()[0]
        response=self.client.post('/api/v1/workers',json={'name':'ignored-foreign','config_id':own['id'],'revision_id':foreign['factory_revision_id']})
        self.assertEqual(response.status_code,404,response.text)
        body={key:own[key] for key in ('name','profile','factory_revision_id','document')}
        body['cloned_from']=foreign['id']
        self.assertEqual(self.client.post('/api/v1/worker-profiles/validate',json=body).status_code,404)
        document=self.client.get('/api/v1/config/revisions/'+self.revision['id']).json()['document']
        self.assertEqual(self.client.post('/api/v1/config/validate',json={'document':document,'parent_id':foreign['factory_revision_id']}).status_code,404)

    def test_repository_writes_validate_configured_profiles_without_assuming_simple(self):
        project=self.project('developer-only')
        path=f'/api/v1/projects/{project}'
        repo=self.client.post(path+'/repositories',json={'alias':'backend','owner':'example','name':'old'}).json()
        latest=self.client.get(path+'/config/revisions').json()[0]
        document=self.client.get(path+'/config/revisions/'+latest['id']).json()['document']
        document['workers']={'developer':document['workers']['developer']}
        published=self.client.post(path+'/config/revisions',json={'parent_id':latest['id'],'document':document})
        self.assertEqual(published.status_code,201,published.text)
        with self.subTest(operation='create'):
            response=self.client.post(path+'/repositories',json={'alias':'frontend','owner':'example','name':'web'})
            self.assertEqual(response.status_code,201,response.text)
        with self.subTest(operation='update'):
            response=self.client.put(path+'/repositories/'+repo['id'],json={'alias':'backend','owner':'example','name':'new','parent_id':repo['revision_id']})
            self.assertEqual(response.status_code,200,response.text)
