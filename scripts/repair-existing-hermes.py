#!/usr/bin/env python3
"""Plan or migrate agent-saas deployments, one at a time, retaining all PVCs."""
import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from hermes_runtime import repair_pod_spec, runtime_configmap, runtime_settings


def plan(deployment):
    """Produce a guarded merge patch without altering the source object."""
    name = deployment["metadata"]["name"]
    template = json.loads(json.dumps(deployment["spec"]["template"]))
    template["spec"] = repair_pod_spec(template["spec"], name)
    return {"metadata": {"resourceVersion": deployment["metadata"]["resourceVersion"]},
            "spec": {"strategy": {"type": "Recreate", "rollingUpdate": None}, "template": template}}


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("namespace", nargs="?", default="demo1")
    parser.add_argument("--deployment", action="append", help="Canary deployment name; repeat to select instances")
    parser.add_argument("--apply", action="store_true", help="Apply the plan (default is read-only)")
    parser.add_argument("--backup-dir", default="hermes-runtime-backups/" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--timeout", default="25m")
    args = parser.parse_args(argv)
    runtime_settings()
    base = ["kubectl", "-n", args.namespace]

    def kubectl(*command, input=None):
        return subprocess.run(base + list(command), input=input, text=True, check=True,
                              stdout=subprocess.PIPE).stdout

    deployments = json.loads(kubectl("get", "deployment", "-l", "app=agent-instance", "-o", "json"))["items"]
    if args.deployment:
        selected = set(args.deployment)
        deployments = [d for d in deployments if d["metadata"]["name"] in selected]
        missing = selected - {d["metadata"]["name"] for d in deployments}
        if missing:
            raise ValueError("Unknown agent-instance deployments: " + ", ".join(sorted(missing)))
    # Validate every target before the first mutation. Never apply a partial plan
    # because a later deployment had an unsupported layout or missing API key.
    plans = [(d, plan(d)) for d in sorted(deployments, key=lambda d: d["metadata"]["name"])]
    for deployment, _ in plans:
        print(("Migrate" if args.apply else "Plan") + ": " + deployment["metadata"]["name"], flush=True)
    if not args.apply or not plans:
        print("No changes applied." if not args.apply else "No matching deployments.")
        return
    backup_dir = pathlib.Path(args.backup_dir)
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    for deployment, patch in plans:
        name = deployment["metadata"]["name"]
        private_json(backup_dir / (name + ".before.json"), deployment)
        private_json(backup_dir / (name + ".patch.json"), patch)
    print("Deployment backups: " + str(backup_dir), flush=True)
    for deployment, _ in plans:
        name = deployment["metadata"]["name"]
        configmap = runtime_configmap(name, args.namespace)
        # Immutable revisioned ConfigMaps keep rollout undo usable: a later
        # runtime revision cannot overwrite the previous revision's scripts.
        kubectl("apply", "-f", "-", input=json.dumps(configmap))
        kubectl("patch", "deployment", name, "--type=merge", "--patch-file",
                str(backup_dir / (name + ".patch.json")))
        try:
            kubectl("rollout", "status", "deployment/" + name, "--timeout=" + args.timeout)
        except subprocess.CalledProcessError:
            print("Rollout failed for " + name + "; restoring its previous template and stopping.", file=sys.stderr)
            current = json.loads(kubectl("get", "deployment", name, "-o", "json"))
            rollback = {"metadata": {"resourceVersion": current["metadata"]["resourceVersion"]},
                        "spec": {"template": deployment["spec"]["template"],
                                 "strategy": dict(deployment["spec"].get("strategy", {}))}}
            if rollback["spec"]["strategy"].get("type") == "Recreate":
                rollback["spec"]["strategy"]["rollingUpdate"] = None
            with tempfile.TemporaryDirectory() as temp:
                file = pathlib.Path(temp) / "rollback.json"
                private_json(file, rollback)
                kubectl("patch", "deployment", name, "--type=merge", "--patch-file", str(file))
            raise
        print("Ready: " + name, flush=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as exc:
        print("Migration stopped: " + str(exc), file=sys.stderr)
        sys.exit(1)
