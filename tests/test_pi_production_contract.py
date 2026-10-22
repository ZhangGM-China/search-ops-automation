from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import yaml
from scripts.release import tke_release
from src.query_governance.pi_governance import PiGovernanceClient

ROOT = Path(__file__).resolve().parents[1]


def test_pi_sidecars_are_released_with_backend_and_gate_readiness():
    docs = list(yaml.safe_load_all(tke_release.render_slot_manifests(
        manifests_dir=ROOT / 'deploy/kubernetes/base', release_sha='a' * 40,
        backend_image='registry.example.test/aiops/backend@sha256:' + '1' * 64,
        frontend_image='registry.example.test/aiops/frontend@sha256:' + '2' * 64,
        schema_release='zha33', artifact_manifest_sha256='3' * 64,
        slot='green', scheduler_mode='standby', include_public_entrypoints=False,
    )))
    for name in ('backend', 'scheduler'):
        pod = next(d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'] == name + '-green')['spec']['template']['spec']
        main = next(c for c in pod['containers'] if c['name'] == name)
        pi = next((c for c in pod['containers'] if c['name'] == 'pi-runtime'), None)
        assert pi is not None, 'PI runtime must actually be deployed'
        assert pi['image'] == main['image']
        assert pi['readinessProbe']['exec']['command']
        assert pi['startupProbe']['exec']['command']
        assert pi['securityContext']['readOnlyRootFilesystem']
        assert not pi.get('envFrom'), 'PI must not receive database/business credentials'
        env = {v['name']: v for v in pi['env']}
        assert env['PI_RUNTIME_HOST']['value'] == '127.0.0.1'
        assert env['KG_AGENT_MCP_TOKEN']['valueFrom']['secretKeyRef']['optional'] is False
        assert not any(v.get('mountPath') == '/app/data' for v in pi.get('volumeMounts', []))
        main_env = {v['name']: v for v in main['env']}
        assert main_env['PI_AGENT_RUNTIME_URL']['value'] == 'http://127.0.0.1:8124'
        assert main_env['PI_AGENT_INTERNAL_TOKEN']['valueFrom'] == env['PI_AGENT_INTERNAL_TOKEN']['valueFrom']


@pytest.mark.parametrize(('exc', 'code'), [
    (httpx.ConnectError('secret-token'), 'PI_RUNTIME_UNAVAILABLE'),
    (httpx.ReadTimeout('secret-token'), 'PI_RUNTIME_TIMEOUT'),
    (ValueError('secret-token'), 'PI_OUTPUT_INVALID'),
])
def test_pi_failure_classification_is_actionable_and_redacted(monkeypatch, exc, code):
    monkeypatch.setenv('RD_LLM_API_KEY', 'fake-model-key')
    runtime = SimpleNamespace(model_config=SimpleNamespace(primary_model='test', fallback_model=None))
    with pytest.raises(RuntimeError) as error:
        PiGovernanceClient(post=Mock(side_effect=exc)).run(runtime=runtime, brand='atlas', scene_id='s', request={}, system_prompt='test', output_schema={})
    assert getattr(error.value, 'error_code', None) == code
    assert 'secret-token' not in str(error.value)


def test_pi_deployment_secrets_required():
    assert {'KG_AGENT_MCP_TOKEN', 'PI_AGENT_INTERNAL_TOKEN'} <= tke_release.REQUIRED_SECRET_KEYS
