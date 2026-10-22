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
