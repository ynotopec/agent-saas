import os
import pathlib
import shutil
import subprocess

import pytest
import yaml

import hermes_runtime as runtime

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELM = os.environ.get("HELM") or shutil.which("helm")


def test_chart_scripts_match_shared_runtime():
    for filename, content in {"start.sh": runtime.START_SCRIPT, "check.py": runtime.CHECK_SCRIPT,
                              "copy-source.sh": runtime.COPY_SOURCE}.items():
        assert (ROOT / "charts/hermes-webui/runtime" / filename).read_text() == content


@pytest.mark.skipif(not HELM, reason="Helm binary required")
@pytest.mark.parametrize("seed", [False, True])
def test_chart_renders_pinned_sources_and_runtime(seed):
    command = [HELM, "template", "canary", str(ROOT / "charts/hermes-webui"),
               "--set-string", "agentImage.digest=sha256:" + "a" * 64,
               "--set-string", "image.digest=sha256:" + "b" * 64]
    if seed:
        command += ["--set", "hermesConfig.seed.enabled=true", "--set-string",
                    "hermesConfig.seed.config=model: {}"]
    output = subprocess.check_output(command, text=True)
    objects = list(yaml.safe_load_all(output))
    deployment = next(o for o in objects if o and o.get("kind") == "Deployment")
    spec = deployment["spec"]["template"]["spec"]
    webui = spec["containers"][0]
    assert "@sha256:" in webui["image"]
    assert spec["initContainers"][-1]["name"] == "init-agent-src"
    assert bool(any(c["name"] == "seed-hermes-config" for c in spec["initContainers"])) == seed
    assert webui["startupProbe"]["exec"]["command"][-1] == "--health"
    runtime_map = next(o for o in objects if o and o.get("kind") == "ConfigMap")
    assert runtime_map["immutable"] is True
    assert runtime_map["data"]["check.py"] == runtime.CHECK_SCRIPT


@pytest.mark.skipif(not HELM, reason="Helm binary required")
def test_chart_rejects_missing_digests():
    result = subprocess.run([HELM, "template", "canary", str(ROOT / "charts/hermes-webui")],
                            capture_output=True, text=True)
    assert result.returncode != 0 and "digest" in result.stderr
