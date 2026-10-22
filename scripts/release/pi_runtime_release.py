"""Provision only absent PI credentials through the authorized release CLI."""
import base64
import json
import os
import secrets
import stat
from pathlib import Path

from dotenv import dotenv_values
from scripts.release import tke_release as tke


def provision(*, env_file: Path, namespace: str, release_sha: str, kube_context: str,
              execute: bool, confirm_release_sha: str | None, runner=None) -> dict:
    tke._require_execute(execute, confirm_release_sha, release_sha)
    if tke.SHA40.fullmatch(release_sha) is None or namespace != 'aiops-demo':
        raise tke.TkeReleaseError('PI credential target is invalid')
    path = Path(env_file)
    if not path.is_absolute() or path.is_symlink():
        raise tke.TkeReleaseError('PI credential file must be a private absolute file')
    node = path.stat()
    if not stat.S_ISREG(node.st_mode) or node.st_uid != os.geteuid() or stat.S_IMODE(node.st_mode) != 0o600:
        raise tke.TkeReleaseError('PI credential file must be owner-only (0600)')
    tke._verify_target_context(kube_context=kube_context, runner=runner)
    template = '{{.metadata.resourceVersion}}{{"\\n"}}{{range $k,$v := .data}}{{$k}}{{"\\n"}}{{end}}'
    raw = tke._require_success(tke._execute(runner, tke._kubectl(kube_context, 'get', 'secret',
        tke.RUNTIME_SECRET_NAME, '-n', namespace, '-o', 'go-template='+template), timeout_seconds=30), 'pi_secret_keys')
    lines = raw.decode().splitlines()
    if not lines or not lines[0]:
        raise tke.TkeReleaseError('PI secret version missing')
    keys = set(lines[1:])
    missing = {'KG_AGENT_MCP_TOKEN', 'PI_AGENT_INTERNAL_TOKEN'} - keys
    if not missing:
        return {'status':'unchanged','addedKeys':[]}
    values = dotenv_values(path)
    token = values.get('KG_AGENT_MCP_TOKEN')
    if 'KG_AGENT_MCP_TOKEN' in missing and not token:
        raise tke.TkeReleaseError('PI credential file lacks KG_AGENT_MCP_TOKEN')
    additions = {'KG_AGENT_MCP_TOKEN':token, 'PI_AGENT_INTERNAL_TOKEN':secrets.token_urlsafe(48)}
    patch = [{'op':'test','path':'/metadata/resourceVersion','value':lines[0]}]
    for key in sorted(missing):
        patch.append({'op':'add','path':'/data/'+key,'value':base64.b64encode(additions[key].encode()).decode()})
    tke._require_success(tke._execute(runner, tke._kubectl(kube_context, 'patch', 'secret',
        tke.RUNTIME_SECRET_NAME, '-n', namespace, '--type=json', '--patch-file=/dev/stdin', '-o', 'name'),
        input_bytes=json.dumps(patch).encode(), timeout_seconds=30), 'pi_secret_provision')
    return {'status':'provisioned','addedKeys':sorted(missing)}
