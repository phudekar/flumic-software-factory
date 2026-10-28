"""Real Job fault injection at the SDK reply boundary, not a fake runtime."""

import copy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import tempfile
import time
import uuid


def run_fault_cases(case):
    from project_release_fixture import ProjectReleaseFixture
    from app.adapters.kubernetes_api import KubernetesApi, KubernetesError
    from app.adapters.kubernetes_jobs import KubernetesJobRuntime
    from app.database import RuntimeJob, RuntimeResourceBinding
    from app.job_broker import JobBroker
    from app.project_context import decrypt
    from app.projects import ProjectScope
    from app.runtime_router import RuntimeRouter
    from app.runtime_targets import TargetRegistry
    from sqlalchemy import select
    from integration.kubernetes_live_fixture import MARKER

    case.assertTrue(
        os.environ.get("CONTROL_TEST_DATABASE_URL", "").startswith("postgresql")
    )
    config = case.config
    root = Path(__file__).resolve().parents[2]
    prerequisite = json.loads(
        (
            root / "output/kubernetes-evidence/qualification-prerequisites.json"
        ).read_text()
    )
    case.assertEqual(prerequisite["cluster_uid"], config.cluster_uid)
    private = Path(
        tempfile.mkdtemp(
            prefix="job-cases-",
            dir=root / ".superpowers/sdd/2026-10-27-kubernetes-runtime",
        )
    )
    target = json.loads((root / "deploy/kubernetes/target.example.json").read_text())[
        "targets"
    ][0]
    image = os.environ["FLUMIC_KUBERNETES_HELPER_IMAGE"]
    worker_ns = next(n for n in config.namespace_uids if "-worker-" in n)
    job_ns = next(n for n in config.namespace_uids if "-action-" in n)
    target.update(
        {
            "auth": {
                "mode": "kubeconfig",
                "kubeconfig": os.environ.get(
                    "FLUMIC_KUBERNETES_CONTROLLER_KUBECONFIG", config.kubeconfig
                ),
                "context": config.context,
            },
            "worker_namespace": worker_ns,
            "worker_namespace_uid": config.namespace_uids[worker_ns],
            "job_namespace": job_ns,
            "job_namespace_uid": config.namespace_uids[job_ns],
            "images": {"simple": image, "developer": image, "helper": image},
            "network_qualified": True,
            "network_evidence": "qualification-prerequisites.json",
            "pid_qualified": True,
            "pid_evidence": "qualification-prerequisites.json",
            "network_policy_names": {"none": "fault-action-deny"},
        }
    )

    def configure(settings):
        path = private / "targets.json"
        path.write_text(json.dumps({"schema_version": 1, "targets": [target]}))
        path.chmod(0o600)
        return replace(settings, runtime_targets_file=path)

    program = """import hashlib,json,time,sys
v=json.load(open('/flumic/input.json'));c=json.load(open('/flumic/context.json'));s=json.load(open('/flumic/credentials.json'))
assert hashlib.sha256(s['openai'].encode()).hexdigest()==v['grant_hash']
assert c['project_id']==v['project_id'];mode=v['delay_seconds']
print(s['openai'],flush=True)
if mode==1:sys.exit(7)
if mode==2:
 open('/flumic/output/result.json','w').write('malformed');sys.exit(0)
if mode==3:
 allocation=bytearray(128*1024*1024);time.sleep(2)
if mode in (4,5):time.sleep(30)
if mode==6:time.sleep(20)
owner=v['left'][0]
json.dump({'output':{'owner':owner,'repository_id':v['repository_id']},'usage':[{'id':'job-call','provider':'fixture','model':'fixture-'+owner,'input_tokens':3,'output_tokens':4,'reported_cost_usd':0.02}]},open('/flumic/output/result.json','w'))
"""
    case.fixture.create(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": "fault-action-deny",
                "namespace": job_ns,
                "labels": {MARKER: config.fixture_id},
            },
            "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
        }
    )
    f = ProjectReleaseFixture(
        settings_transform=configure,
        worker_target_id=target["id"],
        image=image,
        command=["python", "-I", "-c", program],
        directory=private,
    )
    # Keep private recovery authority until native cleanup is actually confirmed.
    recovery = asdict(f.settings)
    recovery["database_url"] = f.case.test_engine.url.render_as_string(
        hide_password=False
    )
    path = private / "recovery.json"
    path.write_text(json.dumps(recovery, default=str))
    path.chmod(0o600)
    faults = {"create": False, "capture": False, "initial": False}
    restarted = {"value": False}
    creations = {}
    captures = {}

    def factory(snapshot, resources, claim):
        if faults["initial"]:
            faults["initial"] = False
            raise KubernetesError(503)
        api = KubernetesApi(snapshot)
        raw = api._raw

        def observed(group, method, **kwargs):
            value = raw(group, method, **kwargs)
            if method == "create_namespaced_job":
                creations.setdefault(value["metadata"]["name"], set()).add(
                    value["metadata"]["uid"]
                )
                if faults["create"]:
                    faults["create"] = False
                    raise KubernetesError(503)
            return value

        api._raw = observed
        native = KubernetesJobRuntime(
            snapshot, resources, api=api, claim=claim, clock=f.clock
        )
        collect = native.collect

        def capture(ref):
            value = collect(ref)
            captures[ref.job_id] = captures.get(ref.job_id, 0) + 1
            if faults["capture"]:
                faults["capture"] = False
                raise KubernetesError(503)
            return value

        native.collect = capture
        return native

    router = RuntimeRouter(
        f.settings, f.config, f.sessions, factories={"kubernetes_jobs": factory}
    )
    router.clock = f.clock
    f.runtime.broker = JobBroker(
        f.sessions,
        f.settings,
        targets=TargetRegistry(f.settings),
        clock=f.clock,
        root=f.root / "jobs",
    )
    f.jobs = router

    def row(job_id):
        with f.sessions() as db:
            return db.get(RuntimeJob, job_id)

    def tick():
        f.runtime.broker.reconcile_once(router)
        if creations and not restarted["value"]:
            # Fresh controller/store/broker objects recover solely from the database.
            f.restart_controller_and_broker()
            restarted["value"] = True
        f.runtime.reconcile_background()
        f.drain_history()

    def wait(predicate, timeout=50):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            tick()
            value = predicate()
            if value:
                return value
            time.sleep(0.3)
        raise AssertionError("Native fault case did not settle")

    def pod_for(job_id):
        with f.sessions() as db:
            binding = db.scalar(
                select(RuntimeResourceBinding).where(
                    RuntimeResourceBinding.job_id == job_id,
                    RuntimeResourceBinding.kind == "Job",
                )
            )
        if not binding or not binding.uid:
            return None
        case.fixture.guard()
        pods = case.fixture.client.run(["get", "pods", "-n", job_ns, "-o", "json"])[
            "items"
        ]
        return next(
            (
                p
                for p in pods
                if any(
                    o.get("uid") == binding.uid and o.get("kind") == "Job"
                    for o in p["metadata"].get("ownerReferences", [])
                )
                and p.get("status", {}).get("phase") == "Running"
            ),
            None,
        )

    summaries = []
    all_clean = False
    try:
        for mode, label, expected in [
            (0, "lost-create-and-capture-replies", "completed"),
            (1, "nonzero-exit", "failed"),
            (2, "malformed-output", "failed"),
            (3, "oom", "failed"),
            (4, "cancel-without-submitter", "cancelled"),
            (5, "deadline-during-controller-outage", "timed-out"),
            (6, "collector-crash", "failed"),
        ]:
            project = f.projects["release-alpha"]
            graph = copy.deepcopy(project["graph"])
            stage = next(stage for stage in graph["stages"] if stage["id"] == "collect")
            stage["limits"] = {
                "memory_mib": 64,
                "pids": 128,
                "cpu": 0.1,
                "tmpfs_mib": 16,
            }
            stage["timeout_seconds"] = 15 if mode == 5 else 120
            project["definition"] = f.call(
                project["base"]
                + "/workflow-definitions/"
                + project["definition"]["id"],
                {"document": graph, "parent_id": project["definition"]["revision_id"]},
                method="PUT",
            )
            project["graph"] = graph
            faults["initial"] = faults["create"] = faults["capture"] = mode == 0
            run = f.launch(
                "release-alpha", key="fault-" + str(uuid.uuid4()), delay_seconds=mode
            )
            job_id = f.until_job(run)
            from app.database import Assignment
            from app.worker_pools import lease_token

            with f.sessions() as db:
                accepted = db.get(RuntimeJob, job_id)
                assignment = db.get(Assignment, accepted.assignment_id)
                duplicate = f.client.post(
                    "/api/v1/shared-worker/assignments/" + assignment.id + "/jobs",
                    json={
                        "lease_token": lease_token(f.settings, assignment),
                        "spec_digest": accepted.spec_digest,
                    },
                    headers=f.headers["developer"],
                )
            case.assertEqual(duplicate.status_code, 202)
            case.assertEqual(duplicate.json()["job_id"], job_id)
            if mode in (4, 5, 6):
                pod = wait(lambda: pod_for(job_id))
                if mode == 4:
                    f.runtime.broker.cancel(ProjectScope(run.project_id), job_id)
                elif mode == 5:
                    time.sleep(18)
                else:
                    # Operator-only runtime stop for this exact owned collector; no node authority in the application.
                    import subprocess

                    case.fixture.guard()
                    current = case.fixture.client.get(
                        "Pod", job_ns, pod["metadata"]["name"]
                    )
                    case.assertEqual(current["metadata"]["uid"], pod["metadata"]["uid"])
                    collector = next(
                        value
                        for value in current["status"]["containerStatuses"]
                        if value["name"] == "collector"
                    )
                    container_id = collector["containerID"].removeprefix(
                        "containerd://"
                    )
                    case.assertRegex(container_id, r"^[a-f0-9]{64}$")
                    node = current["spec"]["nodeName"]
                    case.assertIn(
                        node,
                        {"k3d-flumic-issue17-agent-0", "k3d-flumic-issue17-server-0"},
                    )
                    docker = [
                        "docker",
                        "--context",
                        os.environ["FLUMIC_KUBERNETES_DOCKER_CONTEXT"],
                    ]
                    node_record = json.loads(
                        subprocess.check_output(docker + ["inspect", node], text=True)
                    )[0]
                    case.assertEqual(
                        node_record["Config"]["Labels"].get("k3d.cluster"),
                        "flumic-issue17",
                    )
                    case.fixture.guard()
                    result = subprocess.run(
                        docker
                        + [
                            "exec",
                            node,
                            "crictl",
                            "stop",
                            "--timeout",
                            "0",
                            container_id,
                        ],
                        capture_output=True,
                        timeout=15,
                    )
                    case.assertEqual(
                        result.returncode, 0, "Owned collector runtime stop failed"
                    )
            terminal = wait(
                lambda: (
                    value
                    if (value := row(job_id)).state
                    in {"completed", "failed", "timed-out", "cancelled"}
                    else None
                )
            )
            case.assertEqual(terminal.state, expected, label)
            result = (
                decrypt(terminal.result_ciphertext, f.settings)
                if terminal.result_ciphertext
                else {}
            )
            if mode == 0:
                case.assertGreaterEqual(captures[job_id], 2)
                case.assertFalse(
                    faults["initial"] or faults["create"] or faults["capture"]
                )
                case.assertTrue(restarted["value"])
                case.assertEqual(result["output"]["owner"], "release-alpha")
            elif mode == 1:
                case.assertEqual(result.get("exit_code"), 7)
            elif mode == 3:
                case.assertTrue(result.get("oom"))
            elif mode in (2, 6):
                case.assertTrue(result.get("error"))
            wait(lambda: not row(job_id).cleanup_pending)
            summaries.append(
                {
                    "case": label,
                    "job_id": job_id,
                    "state": terminal.state,
                    "capture_attempts": captures.get(job_id, 0),
                    "cleanup_pending": False,
                }
            )
        case.assertTrue(all(len(values) == 1 for values in creations.values()))
        all_clean = True
    finally:
        for _ in range(40):
            tick()
            with f.sessions() as db:
                pending = [
                    r
                    for r in db.scalars(select(RuntimeJob))
                    if r.state not in {"completed", "failed", "timed-out", "cancelled"}
                    or r.cleanup_pending
                ]
            if not pending:
                break
            time.sleep(0.3)
        if pending:
            raise AssertionError(
                "Native fault-case authority retained in private recovery file; cleanup unresolved"
            )
        f.close()
        case.fixture.cleanup()
        import shutil

        shutil.rmtree(private)
    case.assertTrue(all_clean)
    (root / "output/kubernetes-evidence/qualification-job-faults.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fixture_id": config.fixture_id,
                "cluster_uid": config.cluster_uid,
                "cases": summaries,
                "one_root_uid_per_create": True,
                "controller_restart_recovered": restarted["value"],
                "duplicate_acceptance_idempotent": True,
                "initial_adapter_outage_recovered": True,
                "source_host_writes": 0,
                "paid_model_calls": 0,
            },
            indent=2,
        )
        + "\n"
    )
