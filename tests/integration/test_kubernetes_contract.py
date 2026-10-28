"""Real Kubernetes qualification, never enabled implicitly by the normal suite."""

import unittest

from integration.kubernetes_live_fixture import (
    KubernetesLiveFixture,
    KubectlFixtureClient,
    LiveKubernetesConfig,
    MARKER,
)


class KubernetesContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = LiveKubernetesConfig.from_env()
        import os
        from integration.kubernetes_live_fixture import acquire_fixture_lock

        checkpoint = os.environ.get("FLUMIC_KUBERNETES_CHECKPOINT", "")
        if not checkpoint or not __import__("pathlib").Path(checkpoint).is_absolute():
            raise ValueError("Explicit private checkpoint path required")
        cls.lock = acquire_fixture_lock(checkpoint + ".lock")
        cls.addClassCleanup(cls.lock.close)
        cls.fixture = KubernetesLiveFixture(
            cls.config,
            KubectlFixtureClient(cls.config),
            checkpoint_path=os.environ.get("FLUMIC_KUBERNETES_CHECKPOINT"),
        )
        cls.fixture.guard()

    def test_guarded_configmap_lifecycle(self):
        namespace = next(
            name for name in self.config.namespace_uids if "-action-" in name
        )
        self.fixture.create(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "qualification-proof",
                    "namespace": namespace,
                    "labels": {MARKER: self.config.fixture_id},
                },
                "data": {"proof": "bounded-fixture"},
            }
        )
        self.assertEqual(len(self.fixture.checkpoint()), 1)
        self.fixture.cleanup()
        self.assertEqual(self.fixture.checkpoint(), [])

    def test_00_pid_and_network_enforcement_on_both_nodes(self):
        import os
        import json
        from pathlib import Path
        import subprocess
        import time

        fixture = self.fixture
        client = fixture.client
        worker_ns = next(n for n in self.config.namespace_uids if "-worker-" in n)
        action_ns = next(n for n in self.config.namespace_uids if "-action-" in n)
        image = os.environ.get("FLUMIC_KUBERNETES_PROBE_IMAGE", "")
        self.assertRegex(image, r"^localhost:5117/flumic-probe@sha256:[a-f0-9]{64}$")
        nodes = client.run(["get", "nodes", "-o", "json"])["items"]
        names = sorted(item["metadata"]["name"] for item in nodes)
        self.assertEqual(
            names, ["k3d-flumic-issue17-agent-0", "k3d-flumic-issue17-server-0"]
        )
        helper_image = os.environ.get("FLUMIC_KUBERNETES_HELPER_IMAGE", "")
        self.assertRegex(
            helper_image, r"^localhost:5117/flumic-helper@sha256:[a-f0-9]{64}$"
        )
        observations = []

        def metadata(name, namespace, **labels):
            return {
                "name": name,
                "namespace": namespace,
                "labels": {MARKER: self.config.fixture_id, **labels},
            }

        def pod(name, namespace, node, command, network):
            return {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": metadata(
                    name,
                    namespace,
                    **{
                        "flumic.io/probe": "server"
                        if namespace == worker_ns
                        else "client",
                        "flumic.io/action-network": network,
                    },
                ),
                "spec": {
                    "nodeSelector": {"kubernetes.io/hostname": node},
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "activeDeadlineSeconds": 300,
                    "securityContext": {
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "runAsNonRoot": True,
                    },
                    "containers": [
                        {
                            "name": "probe",
                            "image": image,
                            "command": command,
                            "resources": {
                                "requests": {"cpu": "25m", "memory": "16Mi"},
                                "limits": {"cpu": "100m", "memory": "32Mi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                        }
                    ],
                },
            }

        def ready(name, namespace):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                value = fixture.observe("Pod", namespace, name)
                if value.get("status", {}).get("phase") == "Running":
                    return value
                if value.get("status", {}).get("phase") in {"Failed", "Succeeded"}:
                    self.fail("Fixture probe terminated before observation")
                time.sleep(1)
            self.fail("Fixture probe did not become Running")

        def execute(name, namespace, command):
            fixture.guard()
            result = subprocess.run(
                client.base + ["exec", "-n", namespace, name, "--", *command],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertLess(len(result.stdout) + len(result.stderr), 4096)
            return result

        try:
            fixture.create(
                {
                    "apiVersion": "networking.k8s.io/v1",
                    "kind": "NetworkPolicy",
                    "metadata": metadata("probe-deny", action_ns),
                    "spec": {
                        "podSelector": {},
                        "policyTypes": ["Ingress", "Egress"],
                        "ingress": [],
                        "egress": [],
                    },
                }
            )
            fixture.create(
                {
                    "apiVersion": "networking.k8s.io/v1",
                    "kind": "NetworkPolicy",
                    "metadata": metadata("probe-approved", action_ns),
                    "spec": {
                        "podSelector": {
                            "matchLabels": {"flumic.io/action-network": "bridge"}
                        },
                        "policyTypes": ["Egress"],
                        "egress": [
                            {
                                "to": [
                                    {
                                        "namespaceSelector": {
                                            "matchLabels": {
                                                "kubernetes.io/metadata.name": worker_ns
                                            }
                                        },
                                        "podSelector": {
                                            "matchLabels": {"flumic.io/probe": "server"}
                                        },
                                    }
                                ],
                                "ports": [{"protocol": "TCP", "port": 8080}],
                            }
                        ],
                    },
                }
            )
            servers = []
            for index, node in enumerate(names):
                name = "probe-server-" + str(index)
                fixture.create(
                    pod(
                        name,
                        worker_ns,
                        node,
                        [
                            "sh",
                            "-c",
                            "mkdir /tmp/http && echo flumic-approved > /tmp/http/index.html && exec httpd -f -p 8080 -h /tmp/http",
                        ],
                        "none",
                    )
                )
                servers.append((name, ready(name, worker_ns)["status"]["podIP"]))
            for index, node in enumerate(names):
                name = "probe-pids-" + str(index)
                program = """import errno,json,os,signal,time
children=[];blocked=False
try:
 for _ in range(140):
  try: child=os.fork()
  except BlockingIOError as error:
   assert error.errno==errno.EAGAIN
   blocked=True;break
  if child==0:
   time.sleep(5);os._exit(0)
  children.append(child)
finally:
 for child in children:
  try:os.kill(child,signal.SIGTERM)
  except ProcessLookupError:pass
 for child in children:os.waitpid(child,0)
print(json.dumps({'spawned':len(children),'blocked':blocked}),flush=True)
time.sleep(120)
"""
                value = pod(
                    name, worker_ns, node, ["python", "-I", "-c", program], "none"
                )
                value["spec"]["containers"][0]["image"] = helper_image
                value["spec"]["containers"][0]["resources"]["limits"]["memory"] = "64Mi"
                fixture.create(value)
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    observed = fixture.observe("Pod", worker_ns, name)
                    if observed.get("status", {}).get("phase") == "Failed":
                        self.fail("PID probe failed before proving process enforcement")
                    if observed.get("status", {}).get("phase") == "Running":
                        try:
                            proof = client.run(
                                ["logs", "-n", worker_ns, name, "--limit-bytes=1024"]
                            )
                            break
                        except ValueError:
                            pass
                    time.sleep(1)
                else:
                    self.fail("PID probe did not produce bounded proof")
                self.assertTrue(proof["blocked"])
                self.assertGreater(proof["spawned"], 100)
                self.assertLess(proof["spawned"], 128)
                # Operator-only observation proves the exact parent Pod cgroup ceiling.
                # The application controller never receives node or proxy privileges.
                pod_uid = observed["metadata"]["uid"]
                fixture.guard()
                operator = subprocess.run(
                    [
                        "docker",
                        "--context",
                        os.environ["FLUMIC_KUBERNETES_DOCKER_CONTEXT"],
                        "exec",
                        node,
                        "cat",
                        "/sys/fs/cgroup/kubepods/burstable/pod" + pod_uid + "/pids.max",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(operator.returncode, 0)
                self.assertEqual(operator.stdout.strip(), "128")
                observations.append(
                    {
                        "node": node,
                        "pid_probe_spawned": proof["spawned"],
                        "fork_rejected": True,
                        "parent_pod_pid_ceiling": 128,
                    }
                )
            # A positive control proves that denied DNS/external endpoints are reachable
            # from an unconstrained fixture Pod, not merely unavailable globally.
            control = execute(
                servers[0][0],
                worker_ns,
                ["nslookup", "kubernetes.default.svc.cluster.local"],
            )
            self.assertEqual(control.returncode, 0, "Unconstrained DNS control failed")
            control = execute(
                servers[0][0], worker_ns, ["nc", "-z", "-w", "2", "1.1.1.1", "443"]
            )
            self.assertEqual(
                control.returncode, 0, "Unconstrained external control failed"
            )
            for index, node in enumerate(names):
                for mode in ["none", "bridge"]:
                    name = "probe-client-" + str(index) + "-" + mode
                    fixture.create(pod(name, action_ns, node, ["sleep", "240"], mode))
                    ready(name, action_ns)
                    ceiling = execute(
                        name, action_ns, ["cat", "/sys/fs/cgroup/pids.max"]
                    )
                    self.assertEqual(ceiling.returncode, 0)
                    self.assertIn(ceiling.stdout.strip(), {"max", "128"})
                    for _, ip in servers:
                        response = execute(
                            name,
                            action_ns,
                            [
                                "wget",
                                "-q",
                                "-T",
                                "2",
                                "-O",
                                "-",
                                "http://" + ip + ":8080",
                            ],
                        )
                        if mode == "bridge":
                            self.assertEqual(
                                response.returncode,
                                0,
                                "Approved cross-node egress must work",
                            )
                            self.assertEqual(response.stdout.strip(), "flumic-approved")
                        else:
                            self.assertNotEqual(
                                response.returncode, 0, "Default-deny leaked Pod egress"
                            )
                    external = execute(
                        name, action_ns, ["nc", "-z", "-w", "2", "1.1.1.1", "443"]
                    )
                    self.assertNotEqual(
                        external.returncode, 0, "Unapproved external egress leaked"
                    )
                    dns = execute(
                        name,
                        action_ns,
                        [
                            "timeout",
                            "4",
                            "nslookup",
                            "kubernetes.default.svc.cluster.local",
                        ],
                    )
                    self.assertNotEqual(
                        dns.returncode, 0, "Unapproved DNS egress leaked"
                    )
                    observations.append(
                        {
                            "node": node,
                            "mode": mode,
                            "pod_pid_ceiling": 128,
                            "approved_egress": mode == "bridge",
                            "dns_denied": True,
                            "external_denied": True,
                        }
                    )
        finally:
            for entry in fixture.checkpoint():
                fixture.observe(entry["kind"], entry["namespace"], entry["name"])
            fixture.cleanup()
        directory = Path("output/kubernetes-evidence")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "qualification-prerequisites.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "fixture_id": self.config.fixture_id,
                    "cluster_uid": self.config.cluster_uid,
                    "probe_image": image,
                    "observations": observations,
                    "cleanup_inventory": fixture.checkpoint(),
                    "limitations": [
                        "Standard NetworkPolicy does not isolate traffic to the Pod's own node; namespace/CNI/node administrators are trusted."
                    ],
                },
                indent=2,
            )
            + "\n"
        )

    def test_two_project_api_broker_and_real_jobs(self):
        """Real PostgreSQL/API/broker/SDK/Jobs; fixed external actions only."""
        import os
        import json
        import time
        from dataclasses import replace
        from pathlib import Path
        from project_release_fixture import ProjectReleaseFixture
        from app.runtime_router import RuntimeRouter
        from app.runtime_targets import TargetRegistry
        from app.job_broker import JobBroker
        from app.database import RuntimeJob, StageAttempt, RuntimeResourceBinding
        from sqlalchemy import select
        from app.adapters.kubernetes_api import KubernetesApi

        self.assertTrue(
            os.environ.get("CONTROL_TEST_DATABASE_URL", "").startswith("postgresql"),
            "Dedicated PostgreSQL fixture required",
        )
        evidence = Path("output/kubernetes-evidence/qualification-prerequisites.json")
        self.assertTrue(
            evidence.exists(),
            "Qualify network and PID enforcement before native Job execution",
        )
        proof = json.loads(evidence.read_text())
        self.assertEqual(proof["cluster_uid"], self.config.cluster_uid)
        self.assertEqual(proof["fixture_id"], self.config.fixture_id)
        image = os.environ["FLUMIC_KUBERNETES_HELPER_IMAGE"]
        target = json.loads(Path("deploy/kubernetes/target.example.json").read_text())[
            "targets"
        ][0]
        target.update(
            {
                "auth": {
                    "mode": "kubeconfig",
                    "kubeconfig": os.environ.get(
                        "FLUMIC_KUBERNETES_CONTROLLER_KUBECONFIG",
                        self.config.kubeconfig,
                    ),
                    "context": self.config.context,
                },
                "worker_namespace": next(
                    n for n in self.config.namespace_uids if "-worker-" in n
                ),
                "job_namespace": next(
                    n for n in self.config.namespace_uids if "-action-" in n
                ),
                "images": {"simple": image, "developer": image, "helper": image},
                "network_qualified": True,
                "network_evidence": "qualification-prerequisites.json",
                "pid_qualified": True,
                "pid_evidence": "qualification-prerequisites.json",
                "network_policy_names": {
                    "none": "project-action-deny",
                    "bridge": "project-action-bridge",
                },
            }
        )
        target["worker_namespace_uid"] = self.config.namespace_uids[
            target["worker_namespace"]
        ]
        target["job_namespace_uid"] = self.config.namespace_uids[
            target["job_namespace"]
        ]

        def configure(settings):
            path = Path(settings.snapshot_root) / "native-targets.json"
            path.write_text(json.dumps({"schema_version": 1, "targets": [target]}))
            path.chmod(0o600)
            return replace(settings, runtime_targets_file=path)

        # Mirrors the existing release action contract; no external provider/source calls.
        program = """import hashlib,json,time
v=json.load(open('/flumic/input.json'));c=json.load(open('/flumic/context.json'));s=json.load(open('/flumic/credentials.json'))
assert set(s)=={'openai'} and hashlib.sha256(s['openai'].encode()).hexdigest()==v['grant_hash']
assert c['project_id']==v['project_id'] and c['repository_id']==v['repository_id']
assert v['left']==v['right'];owner=v['left'][0]
print(s['openai'],flush=True)
json.dump({'output':{'owner':owner,'repository_id':v['repository_id']},'usage':[{'id':'job-call','provider':'fixture','model':'fixture-'+owner,'input_tokens':3,'output_tokens':4,'reported_cost_usd':0.02}]},open('/flumic/output/result.json','w'))
"""
        self.fixture.create(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {
                    "name": "project-action-deny",
                    "namespace": target["job_namespace"],
                    "labels": {MARKER: self.config.fixture_id},
                },
                "spec": {
                    "podSelector": {},
                    "policyTypes": ["Ingress", "Egress"],
                    "ingress": [],
                    "egress": [],
                },
            }
        )
        self.fixture.create(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {
                    "name": "project-action-bridge",
                    "namespace": target["job_namespace"],
                    "labels": {MARKER: self.config.fixture_id},
                },
                "spec": {
                    "podSelector": {
                        "matchLabels": {"flumic.io/action-network": "bridge"}
                    },
                    "policyTypes": ["Egress"],
                    "egress": [],
                },
            }
        )
        f = ProjectReleaseFixture(
            settings_transform=configure,
            worker_target_id=target["id"],
            image=image,
            command=["python", "-I", "-c", program],
        )
        f.runtime.broker = JobBroker(
            f.sessions,
            f.settings,
            targets=TargetRegistry(f.settings),
            clock=f.clock,
            root=f.root / "jobs",
        )
        f.jobs = RuntimeRouter(f.settings, f.config, f.sessions)
        f.jobs.clock = f.clock
        api = KubernetesApi(TargetRegistry(f.settings).snapshot(target["id"]))
        try:
            api._admission_fence("job")
            api._admission_fence("worker")
        finally:
            api.close()
        jobs = []
        safe_to_close = False
        try:
            import copy

            alpha = f.projects["release-alpha"]
            graph = copy.deepcopy(alpha["graph"])
            next(stage for stage in graph["stages"] if stage["id"] == "collect")[
                "network"
            ] = "bridge"
            alpha["definition"] = f.call(
                alpha["base"] + "/workflow-definitions/" + alpha["definition"]["id"],
                {"document": graph, "parent_id": alpha["definition"]["revision_id"]},
                method="PUT",
            )
            alpha["graph"] = graph
            self.assertIsNone(f.settings.job_bridge)
            runs = [f.launch(slug) for slug in f.projects]
            f.rotate("release-alpha")
            # Real scheduling/capture is asynchronous. Avoid a synthetic tight-poll timeout.
            states = []
            for _ in range(90):
                for run in runs:
                    frontier = f.runtime.advance(run)
                    for invocation in frontier.ready:
                        f.execute(
                            invocation, frontier.routes[invocation.execution_id].role
                        )
                f.runtime.broker.reconcile_once(f.jobs)
                f.runtime.reconcile_background()
                f.drain_history()
                states = [f.detail(run)["state"] for run in runs]
                if all(
                    value in {"completed", "failed", "cancelled"} for value in states
                ):
                    break
                time.sleep(0.5)
            self.assertEqual(states, ["completed", "completed"])
            for run in runs:
                project = next(
                    v for v in f.projects.values() if v["id"] == run.project_id
                )
                usage = f.call(project["base"] + "/usage?run_id=" + run.run_id)
                self.assertEqual(
                    (usage["calls"], usage["input_tokens"], usage["output_tokens"]),
                    (2, 4, 6),
                )
                self.assertAlmostEqual(usage["reported_usd"], 0.03)
                with f.sessions() as db:
                    row = db.scalar(
                        select(RuntimeJob)
                        .join(StageAttempt, RuntimeJob.execution_id == StageAttempt.id)
                        .where(StageAttempt.run_id == run.run_id)
                    )
                    jobs.append(row.id)
                    detail = f.call(project["base"] + "/jobs/" + row.id)
                    self.assertEqual(detail["runtime"]["log_stream"], "combined")
                    self.assertTrue(detail["runtime"]["job_uid"])
                    self.assertEqual(len(detail["runtime"]["pod_uids"]), 1)
                    self.assertNotIn(
                        next(
                            slug
                            for slug, value in f.projects.items()
                            if value["id"] == run.project_id
                        )
                        + "-openai-v1",
                        detail["result"]["stdout"],
                    )
                    foreign = next(
                        v["base"]
                        for v in f.projects.values()
                        if v["id"] != run.project_id
                    )
                    self.assertEqual(
                        f.client.get(foreign + "/jobs/" + row.id).status_code, 404
                    )
            safe_to_close = True
        finally:
            # Keep database authority until every owned Job deletion is observed.
            for _ in range(30):
                f.runtime.broker.reconcile_once(f.jobs)
                with f.sessions() as db:
                    rows = list(db.scalars(select(RuntimeJob)))
                    pending = [
                        r
                        for r in rows
                        if r.state
                        not in {"completed", "failed", "timed-out", "cancelled"}
                        or r.cleanup_pending
                    ]
                if not pending:
                    break
                time.sleep(0.5)
            if not pending:
                with f.sessions() as db:
                    bindings = [
                        {"kind": r.kind, "uid": r.uid}
                        for r in db.scalars(select(RuntimeResourceBinding))
                        if r.uid
                    ]
                f.close()
                self.fixture.cleanup()
            else:
                self.fail(
                    "Native Job cleanup incomplete; dedicated fixture database/resources retained for guarded recovery"
                )
        self.assertTrue(safe_to_close)
        Path("output/kubernetes-evidence/qualification-project-jobs.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "fixture_id": self.config.fixture_id,
                    "cluster_uid": self.config.cluster_uid,
                    "projects": 2,
                    "jobs": len(jobs),
                    "native_network_modes": ["none", "bridge"],
                    "usage_calls_per_project": 2,
                    "input_tokens_per_project": 4,
                    "output_tokens_per_project": 6,
                    "reported_usd_per_project": 0.03,
                    "resources": bindings,
                    "real_components": [
                        "PostgreSQL",
                        "HTTP routes",
                        "shared assignment protocol",
                        "native SDK",
                        "Kubernetes Jobs",
                        "init and collector",
                        "bounded combined logs",
                    ],
                    "fixture_components": [
                        "source host and model actions",
                        "in-process shared-worker transport; real Pod worker qualification is separate",
                    ],
                },
                indent=2,
            )
            + "\n"
        )

    def test_worker_pods_register_restart_and_retain_private_volumes(self):
        """Native lifecycle with unchanged shared-v1 workers and isolated API/DB."""
        import base64
        import json
        import os
        from dataclasses import asdict, replace
        from pathlib import Path
        import subprocess
        import tempfile
        import time
        from dotenv import dotenv_values
        from sqlalchemy import select
        from project_release_fixture import ProjectReleaseFixture
        from app.database import Worker, Operation, RuntimeResourceBinding
        from app.runtime_router import RuntimeRouter
        from app.reconciler import reconcile_once

        self.assertTrue(
            os.environ.get("CONTROL_TEST_DATABASE_URL", "").startswith("postgresql")
        )
        worker_image = os.environ.get("FLUMIC_KUBERNETES_WORKER_IMAGE", "")
        self.assertRegex(
            worker_image, r"^localhost:5117/flumic-worker@sha256:[a-f0-9]{64}$"
        )
        docker_context = os.environ.get("FLUMIC_KUBERNETES_DOCKER_CONTEXT", "")
        self.assertTrue(docker_context, "Explicit owned Docker context is required")
        docker = ["docker", "--context", docker_context]
        # Only the already authorized test tenant's Hatchet transport is selected.
        source = dotenv_values(os.environ["FLUMIC_KUBERNETES_TRANSPORT_ENV_FILE"])
        transport = {
            key: value
            for key, value in source.items()
            if key
            in {
                "HATCHET_CLIENT_TOKEN",
                "HATCHET_CLIENT_HOST_PORT",
                "HATCHET_CLIENT_SERVER_URL",
                "HATCHET_CLIENT_TLS_STRATEGY",
            }
            and value
        }
        self.assertTrue(
            transport.get("HATCHET_CLIENT_TOKEN"),
            "Test tenant transport token required before fixture writes",
        )
        worker_ns = next(n for n in self.config.namespace_uids if "-worker-" in n)
        job_ns = next(n for n in self.config.namespace_uids if "-action-" in n)
        client = self.fixture.client
        self.fixture.guard()
        # Verify the owned fixture DB container before adding its private test network.
        pg = json.loads(
            subprocess.check_output(
                docker + ["inspect", "flumic-runtime-test-postgres"], text=True
            )
        )[0]
        self.assertEqual(pg["Name"], "/flumic-runtime-test-postgres")
        connected = "k3d-flumic-issue17" not in pg["NetworkSettings"]["Networks"]
        if connected:
            subprocess.run(
                docker
                + [
                    "network",
                    "connect",
                    "k3d-flumic-issue17",
                    "flumic-runtime-test-postgres",
                ],
                check=True,
                capture_output=True,
            )
        root = Path(__file__).resolve().parents[2]
        private_root = Path(
            tempfile.mkdtemp(
                prefix="worker-pod-",
                dir=root / ".superpowers/sdd/2026-10-27-kubernetes-runtime",
            )
        )
        cfg = private_root / "api.json"
        name = "flumic-i17-control-fixture"
        # Never adopt or replace an existing container with this name.
        existing = subprocess.run(docker + ["inspect", name], capture_output=True)
        self.assertNotEqual(
            existing.returncode,
            0,
            "Fixture API name already exists; inspect ownership before resuming",
        )
        subprocess.run(
            docker
            + [
                "run",
                "-d",
                "--name",
                name,
                "--label",
                "flumic.io/qualification=" + self.config.fixture_id,
                "--network",
                "k3d-flumic-issue17",
                "--mount",
                "type=bind,src=" + str(root) + ",dst=" + str(root) + ",readonly",
                "--env",
                "FLUMIC_LIVE_KUBERNETES=1",
                "--env",
                "FLUMIC_KUBERNETES_API_FIXTURE_CONFIG=" + str(cfg),
                "localhost:5117/flumic-control-fixture:issue17",
                "python",
                str(root / "tests/integration/kubernetes_api_fixture.py"),
            ],
            check=True,
            capture_output=True,
        )
        ip = json.loads(subprocess.check_output(docker + ["inspect", name], text=True))[
            0
        ]["NetworkSettings"]["Networks"]["k3d-flumic-issue17"]["IPAddress"]
        target = json.loads(
            (root / "deploy/kubernetes/target.example.json").read_text()
        )["targets"][0]
        target.update(
            {
                "auth": {
                    "mode": "kubeconfig",
                    "kubeconfig": os.environ.get(
                        "FLUMIC_KUBERNETES_CONTROLLER_KUBECONFIG",
                        self.config.kubeconfig,
                    ),
                    "context": self.config.context,
                },
                "worker_namespace": worker_ns,
                "worker_namespace_uid": self.config.namespace_uids[worker_ns],
                "job_namespace": job_ns,
                "job_namespace_uid": self.config.namespace_uids[job_ns],
                "worker_endpoint": "http://" + ip + ":8000",
                "images": {
                    "simple": worker_image,
                    "developer": worker_image,
                    "helper": os.environ["FLUMIC_KUBERNETES_HELPER_IMAGE"],
                },
                "hatchet_secret_name": "fixture-hatchet-transport",
                "node_selector": {},
            }
        )

        def configure(settings):
            path = private_root / "targets.json"
            path.write_text(json.dumps({"schema_version": 1, "targets": [target]}))
            path.chmod(0o600)
            return replace(settings, runtime_targets_file=path)

        f = None
        observations = []
        clean = False
        try:
            self.fixture.create(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {
                        "name": "fixture-hatchet-transport",
                        "namespace": worker_ns,
                        "labels": {MARKER: self.config.fixture_id},
                    },
                    "type": "Opaque",
                    "data": {
                        k: base64.b64encode(v.encode()).decode()
                        for k, v in transport.items()
                    },
                }
            )
            f = ProjectReleaseFixture(
                settings_transform=configure, worker_target_id=target["id"]
            )
            # Remove in-process fixture registration: only actual Pods may prove it.
            with f.sessions.begin() as db:
                for worker in db.scalars(select(Worker)):
                    worker.registered_at = None
                    worker.last_heartbeat_at = None
                    worker.capabilities = {}
            settings = asdict(f.settings)
            for key, value in list(settings.items()):
                if isinstance(value, Path):
                    settings[key] = str(value)
            settings["database_url"] = f.case.test_engine.url.set(
                host="flumic-runtime-test-postgres", port=5432
            ).render_as_string(hide_password=False)
            # No fixture-host temp paths are needed by this bootstrap-only API.
            settings["snapshot_root"] = str(private_root / "snapshots")
            cfg.write_text(json.dumps(settings))
            cfg.chmod(0o600)
            router = RuntimeRouter(f.settings, f.config, f.sessions)

            def lifecycle(worker_id, verb=None):
                if verb:
                    import uuid

                    f.call(
                        "/api/v1/workers/" + worker_id + "/actions",
                        {"action": verb},
                        202,
                        headers={"Idempotency-Key": str(uuid.uuid4())},
                    )
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    reconcile_once(f.sessions, router, f.settings)
                    with f.sessions() as db:
                        operations = list(
                            db.scalars(
                                select(Operation)
                                .where(Operation.worker_id == worker_id)
                                .order_by(Operation.created_at.desc())
                            )
                        )
                        if operations and operations[0].status == "failed":
                            self.fail(
                                "Native worker lifecycle failed: "
                                + str(operations[0].error)
                            )
                        if operations and all(
                            v.status == "completed" for v in operations
                        ):
                            return
                    time.sleep(0.5)
                self.fail("Worker lifecycle failed to settle")

            def observed(worker_id, *, registered=False):
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    values = router.list()
                    value = next(
                        (v for v in values if v.get("instance_id") == worker_id), None
                    )
                    with f.sessions() as db:
                        row = db.get(Worker, worker_id)
                        seen = row.registered_at is not None
                    if (
                        value
                        and value["status"] == "running"
                        and (seen or not registered)
                    ):
                        return value
                    time.sleep(1)
                self.fail("Native worker readiness/registration not observed")

            for role in ["simple", "developer"]:
                lifecycle(f.workers[role]["id"])
            # Operator-only cordon steers first placements; PVC topology then pins replays.
            nodes = ["k3d-flumic-issue17-agent-0", "k3d-flumic-issue17-server-0"]
            for index, role in enumerate(["simple", "developer"]):
                blocked = nodes[1 - index]
                self.fixture.guard()
                subprocess.run(
                    client.base + ["cordon", blocked], check=True, capture_output=True
                )
                try:
                    worker_id = f.workers[role]["id"]
                    lifecycle(worker_id, "start")
                    value = observed(worker_id, registered=True)
                    with f.sessions() as db:
                        binding = db.scalar(
                            select(RuntimeResourceBinding).where(
                                RuntimeResourceBinding.worker_id == worker_id,
                                RuntimeResourceBinding.kind == "PersistentVolumeClaim",
                            )
                        )
                        pvc_uid = binding.uid
                    pods = client.run(["get", "pods", "-n", worker_ns, "-o", "json"])[
                        "items"
                    ]
                    actual = next(
                        pod
                        for pod in pods
                        if pod["metadata"]["uid"] == value["pod_uids"][0]
                    )
                    self.assertEqual(actual["spec"]["nodeName"], nodes[index])
                    observations.append(
                        {
                            "node": actual["spec"]["nodeName"],
                            "role": role,
                            "worker_id": worker_id,
                            "deployment_uid": value["deployment_uid"],
                            "pod_uid": value["pod_uids"][0],
                            "pvc_uid": pvc_uid,
                            "registered": True,
                        }
                    )
                finally:
                    self.fixture.guard()
                    subprocess.run(
                        client.base + ["uncordon", blocked],
                        check=True,
                        capture_output=True,
                    )
            self.assertNotEqual(observations[0]["pvc_uid"], observations[1]["pvc_uid"])
            from unittest.mock import patch
            from hatchet_sdk import Hatchet
            from app.adapters.hatchet import items, plain

            with patch.dict(os.environ, transport):
                engine = Hatchet()
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    registered = {
                        plain(value).get("name"): plain(value)
                        for value in items(engine.workers.list())
                    }
                    own = [
                        registered.get(f.workers[role]["name"])
                        for role in ["simple", "developer"]
                    ]
                    if all(value and value.get("lastHeartbeatAt") for value in own):
                        break
                    time.sleep(2)
                else:
                    self.fail("Real Hatchet worker registration/heartbeat not observed")
                for value in observations:
                    value["hatchet_heartbeat_observed"] = True
            # Queue two copies of one redacted event while the owned API is paused.
            # Only JSON on stdin carries the lease/context; never CLI arguments/logs.
            import uuid

            run = f.launch("release-alpha")
            frontier = f.runtime.advance(run)
            invocation = frontier.ready[0]
            claim = f.controls["simple"].request(
                "/api/v1/shared-worker/claim",
                {**invocation.model_dump(), "claim_key": str(uuid.uuid4())},
            )
            delivered = f.controls["simple"].request(
                "/api/v1/shared-worker/assignments/"
                + claim["assignment_id"]
                + "/context",
                {"lease_token": claim["lease_token"]},
            )
            simple_id = f.workers["simple"]["id"]
            value = observed(simple_id, registered=True)
            pod = next(
                p
                for p in client.run(["get", "pods", "-n", worker_ns, "-o", "json"])[
                    "items"
                ]
                if p["metadata"]["uid"] == value["pod_uids"][0]
            )
            program = """import json,os,sys
from pathlib import Path
from types import SimpleNamespace
from factory.workflows.contracts import ActionContext
from factory.adapters.shared_history import SharedPublisher
v=json.load(sys.stdin)
context=ActionContext.model_validate_json(json.dumps(v['context'])).with_credentials(v['credentials'])
control=SimpleNamespace(settings=SimpleNamespace(worker_id=os.environ['FACTORY_CONTROL_WORKER_ID']),headers={'Authorization':'Bearer '+os.environ['FACTORY_CONTROL_TOKEN']})
publisher=SharedPublisher(control,Path('/data/control-spool'),start=False)
secret=next(iter(context.credentials.values()))
publisher.bound(context,v['proof']).emit('log',{'message':secret+' replay-proof'},None)
files=list(publisher.root.glob('*.json'));assert len(files)==1
original=files[0];document=json.loads(original.read_text())
assert secret not in json.dumps(document['events'][0]['payload'])
copy=publisher.root/(context.assignment_id+'--duplicate.json');copy.write_text(original.read_text());copy.chmod(0o600)
print(json.dumps({'event_id':document['events'][0]['id'],'queued':2,'redacted':True}))
"""
            subprocess.run(docker + ["pause", name], check=True, capture_output=True)
            try:
                self.fixture.guard()
                result = subprocess.run(
                    client.base
                    + [
                        "exec",
                        "-i",
                        "-n",
                        worker_ns,
                        pod["metadata"]["name"],
                        "--",
                        "python",
                        "-c",
                        program,
                    ],
                    input=json.dumps({**delivered, "proof": claim["lease_token"]}),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(
                    result.returncode, 0, "Private replay queue preparation failed"
                )
                queued = json.loads(result.stdout)
                self.assertEqual(queued["queued"], 2)
                lifecycle(simple_id, "stop")
            finally:
                subprocess.run(
                    docker + ["unpause", name], check=True, capture_output=True
                )
            lifecycle(simple_id, "start")
            observed(simple_id, registered=True)
            from app.database import Event, WorkerPool

            from factory.adapters.shared_history import history_identity

            public_event_id = str(
                uuid.UUID(
                    hex=history_identity(
                        queued["event_id"],
                        f.settings.encryption_key,
                        claim["assignment_id"],
                        domain="event",
                    )[:32],
                    version=4,
                )
            )
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                with f.sessions() as db:
                    events = list(
                        db.scalars(select(Event).where(Event.id == public_event_id))
                    )
                if len(events) == 1:
                    break
                time.sleep(1)
            self.assertEqual(
                len(events),
                1,
                "Retained private spool did not replay exactly one logical event",
            )
            self.assertIn("replay-proof", events[0].payload["message"])
            self.assertNotIn(
                next(iter(delivered["credentials"].values())),
                events[0].payload["message"],
            )
            # Exercise infrastructure reconfiguration through the real durable operation.
            with f.sessions.begin() as db:
                pool = db.get(WorkerPool, f.pools["simple"]["id"])
                pool.profile = {**pool.profile, "normal_slots": 3}
                worker = db.get(Worker, simple_id)
                worker.registered_at = None
                db.add(
                    Operation(
                        worker_id=simple_id,
                        action="configure",
                        key="fixture-configure-" + str(uuid.uuid4()),
                    )
                )
            lifecycle(simple_id)
            lifecycle(simple_id, "start")
            observed(simple_id, registered=True)
            for role in ["simple", "developer"]:
                worker_id = f.workers[role]["id"]
                lifecycle(worker_id, "stop")
                lifecycle(worker_id, "start")
                value = observed(worker_id, registered=True)
                previous = next(v for v in observations if v["role"] == role)
                self.assertEqual(value["deployment_uid"], previous["deployment_uid"])
                self.assertNotEqual(value["pod_uids"][0], previous["pod_uid"])
                self.assertLessEqual(len(router.logs(value["id"], 100)), 65536)
            clean = True
        finally:
            if f:
                for role in ["simple", "developer"]:
                    lifecycle(f.workers[role]["id"], "remove")
                # Retained private PVCs are deliberate; record then guarded fixture removal.
                with f.sessions() as db:
                    retained = list(
                        db.scalars(
                            select(RuntimeResourceBinding).where(
                                RuntimeResourceBinding.kind == "PersistentVolumeClaim"
                            )
                        )
                    )
                for binding in retained:
                    current = client.get(
                        "PersistentVolumeClaim", worker_ns, binding.name
                    )
                    self.assertEqual(current["metadata"]["uid"], binding.uid)
                    self.assertFalse(current["metadata"].get("ownerReferences"))
                    self.fixture.guard()
                    client.delete(
                        "PersistentVolumeClaim",
                        worker_ns,
                        binding.name,
                        uid=binding.uid,
                        resource_version=current["metadata"]["resourceVersion"],
                    )
                f.close()
            self.fixture.cleanup()
            container = json.loads(
                subprocess.check_output(docker + ["inspect", name], text=True)
            )[0]
            self.assertEqual(
                container["Config"]["Labels"].get(MARKER), self.config.fixture_id
            )
            subprocess.run(
                docker + ["rm", "-f", container["Id"]], check=True, capture_output=True
            )
            if connected:
                subprocess.run(
                    docker
                    + [
                        "network",
                        "disconnect",
                        "k3d-flumic-issue17",
                        "flumic-runtime-test-postgres",
                    ],
                    check=True,
                    capture_output=True,
                )
            import shutil

            shutil.rmtree(private_root)
        self.assertTrue(clean)
        (root / "output/kubernetes-evidence/qualification-worker-pods.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "fixture_id": self.config.fixture_id,
                    "cluster_uid": self.config.cluster_uid,
                    "workers": observations,
                    "image": worker_image,
                    "private_pvc_retention": True,
                    "restart_identity_preserved": True,
                    "private_history_replay_deduplicated": True,
                    "infrastructure_reconfiguration": True,
                },
                indent=2,
            )
            + "\n"
        )

    def test_native_job_failures_and_recovery(self):
        from integration.kubernetes_job_cases import run_fault_cases

        run_fault_cases(self)

    def test_namespace_fence_rejects_annotation_stripping_updates(self):
        import subprocess

        worker = next(n for n in self.config.namespace_uids if "-worker-" in n)
        self.fixture.create(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "update-fence-proof",
                    "namespace": worker,
                    "labels": {MARKER: self.config.fixture_id},
                    "annotations": {
                        "flumic.io/namespace-uid": self.config.namespace_uids[worker]
                    },
                },
                "data": {"proof": "safe"},
            }
        )
        value = self.fixture.client.get("ConfigMap", worker, "update-fence-proof")
        import json

        tests = [
            {"op": "test", "path": "/metadata/uid", "value": value["metadata"]["uid"]},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": value["metadata"]["resourceVersion"],
            },
        ]
        try:
            base = self.fixture.client.base + [
                "patch",
                "configmap",
                "update-fence-proof",
                "-n",
                worker,
                "--type=json",
                "--dry-run=server",
                "-o",
                "json",
                "-p",
            ]
            control = subprocess.run(
                base
                + [
                    json.dumps(
                        tests
                        + [{"op": "replace", "path": "/data/proof", "value": "safe"}]
                    )
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(control.returncode, 0)
            denied = subprocess.run(
                base
                + [
                    json.dumps(
                        tests
                        + [
                            {
                                "op": "remove",
                                "path": "/metadata/annotations/flumic.io~1namespace-uid",
                            }
                        ]
                    )
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("Flumic resource namespace incarnation", denied.stderr)
        finally:
            self.fixture.cleanup()

    def test_worker_failure_observations_remain_unhealthy(self):
        from integration.kubernetes_worker_cases import run_worker_failure_cases

        run_worker_failure_cases(self)
