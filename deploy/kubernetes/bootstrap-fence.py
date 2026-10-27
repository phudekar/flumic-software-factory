#!/usr/bin/env python3
"""Pin freshly created namespace incarnations with explicit operator authority."""
import argparse
import json
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--kubeconfig", required=True)
parser.add_argument("--context", required=True)
parser.add_argument("namespaces", nargs="+")
args = parser.parse_args()
base = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
for name in args.namespaces:
    namespace = json.loads(subprocess.check_output(base + ["get", "namespace", name, "-o", "json"]))
    metadata = namespace["metadata"]
    annotations = dict(metadata.get("annotations", {}))
    annotations["flumic.io/namespace-uid"] = metadata["uid"]
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
        {"op": "add", "path": "/metadata/annotations", "value": annotations},
    ]
    subprocess.run(base + ["patch", "namespace", name, "--type=json", "-p", json.dumps(patch)], check=True, stdout=subprocess.DEVNULL)
    print(name + " UID: " + metadata["uid"])
