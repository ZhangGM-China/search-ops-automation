import json
from pathlib import Path
import pytest
from scripts.release import pi_runtime_release as module
from scripts.release import tke_release as tke


def test_provision_only_missing_keys_over_stdin_and_never_print_values(tmp_path, monkeypatch):
    monkeypatch.setattr(tke, '_verify_target_context', lambda **kw: None)
    source = tmp_path / 'pi.env'
    source.write_text('KG_AGENT_MCP_TOKEN=fake-mcp-value\n')
    source.chmod(0o600)
    calls = []
    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if 'get' in argv:
            return tke.CommandResult(0, b'17\nSECRET_KEY\nPI_AGENT_INTERNAL_TOKEN\n', b'')
        return tke.CommandResult(0, b'', b'')
    result = module.provision(env_file=source, namespace='aiops-demo', release_sha='a'*40,
        kube_context='cls-demo0000-test', execute=True, confirm_release_sha='a'*40, runner=runner)
    patch_call = next(c for c in calls if 'patch' in c[0])
    patch = json.loads(patch_call[1]['input_bytes'])
    assert patch[0] == {'op':'test','path':'/metadata/resourceVersion','value':'17'}
    assert [p['path'] for p in patch[1:]] == ['/data/KG_AGENT_MCP_TOKEN']
    assert 'fake-mcp-value' not in str(result)
    assert 'fake-mcp-value' not in str(patch_call[0])


def test_missing_confirmation_never_touches_cluster(tmp_path):
    with pytest.raises(tke.TkeReleaseError):
        module.provision(env_file=tmp_path/'absent', namespace='aiops-demo', release_sha='a'*40,
            kube_context='cls-demo0000-test', execute=False, confirm_release_sha=None,
            runner=lambda *a,**kw: pytest.fail('must not access cluster'))
