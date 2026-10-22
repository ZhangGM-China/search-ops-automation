"""Scoped lifecycle for the isolated image Worker; called only by release.py."""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from scripts.release import tke_candidate

CONTEXT = 'cls-demo0000-000000000000-context-default'
NAMESPACE = 'aiops-demo'
NAME = 'qg-atlas-browser-worker'
REPOSITORY = f'{tke_candidate.REGISTRY_HOST}/aiops/{NAME}'
RESOURCES = [f'{kind}/{NAME}' for kind in ('Deployment', 'Service', 'NetworkPolicy')]
STATE_FILE = 'image-worker-state.json'


class ImageWorkerReleaseError(ValueError):
    """Worker release gate failed; never contains secret values."""


def validate_target(context: str, namespace: str) -> None:
    if (context, namespace) != (CONTEXT, NAMESPACE):
        raise ImageWorkerReleaseError('worker release target is invalid')


def validate_sha(value: str) -> str:
    if not re.fullmatch(r'[0-9a-f]{40}', value or ''):
        raise ImageWorkerReleaseError('release SHA is invalid')
    return value


def validate_image(value: str) -> str:
    if not re.fullmatch(re.escape(REPOSITORY) + r'@sha256:[0-9a-f]{64}', value or ''):
        raise ImageWorkerReleaseError('worker requires an immutable dedicated repository digest')
    return value


def validate_url(value: str) -> str:
    url = urlsplit(value)
    if url.scheme != 'https' or url.hostname != 'static.atlas.example.test' or url.username or url.password or url.port not in (443, None) or not url.path.startswith('/assets/'):
        raise ImageWorkerReleaseError('probe requires a real https://static.atlas.example.test/assets/ image URL')
    return value


def preview(release_sha: str) -> dict:
    return {'releaseSha': validate_sha(release_sha), 'namespace': NAMESPACE,
            'context': CONTEXT, 'platform': 'linux/amd64', 'resources': RESOURCES,
            'repository': REPOSITORY}


def run(command: list[str], *, input_text: str | None = None, timeout: int = 120, sensitive: bool = False) -> str:
    try:
        result = subprocess.run(command, input=input_text, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ImageWorkerReleaseError(f'{command[0]} execution failed') from exc
    if result.returncode:
        detail = '' if sensitive else (result.stderr or result.stdout).strip()[-1500:]
        raise ImageWorkerReleaseError(f'{command[0]} failed: {detail}')
    return result.stdout


def kubectl(*args: str, **kwargs) -> str:
    return run(['kubectl', '--context', CONTEXT, '--request-timeout=20s', '-n', NAMESPACE, *args], **kwargs)


def render(repo: Path, image: str, release_sha: str, probe_url: str | None = None) -> list[dict]:
    import yaml
    validate_image(image)
    validate_sha(release_sha)
    documents = list(yaml.safe_load_all(run(['kubectl', 'kustomize', str(repo / 'scripts/image_sync/online')])))
    if {f"{d['kind']}/{d['metadata']['name']}" for d in documents} != set(RESOURCES) or len(documents) != 3:
        raise ImageWorkerReleaseError('render exceeded the three-resource scope')
    for doc in documents:
        doc['metadata']['namespace'] = NAMESPACE
        doc['metadata'].setdefault('annotations', {})['aiops/release-sha'] = release_sha
        if doc['kind'] == 'Deployment':
            template = doc['spec']['template']
            template['metadata'].setdefault('annotations', {})['aiops/release-sha'] = release_sha
            c = template['spec']['containers'][0]
            c['image'] = image
            if probe_url:
                c['env'].append({'name': 'QG_BROWSER_PROBE_URL', 'value': validate_url(probe_url)})
    return documents


def manifest(documents: list[dict]) -> str:
    return json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': documents})


def write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink() or path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
        raise ImageWorkerReleaseError('state directory must be owner-only and not symlinked')
    temp = path.with_name(path.name + '.' + secrets.token_hex(8))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream)
    except BaseException:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise
    os.replace(temp, path)


def read_state(path: Path, release_sha: str) -> dict:
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ImageWorkerReleaseError('invalid worker state permissions')
    record = json.loads(path.read_text())
    if record.get('releaseSha') != validate_sha(release_sha):
        raise ImageWorkerReleaseError('worker candidate SHA drift')
    validate_image(record.get('image', ''))
    return record


def build_proxy_args() -> list[str]:
    proxy = os.environ.get('AIOPS_WORKER_BUILD_PROXY', '').strip()
    if not proxy:
        return []
    parts = urlsplit(proxy)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ImageWorkerReleaseError('build proxy must be an HTTP(S) endpoint without credentials')
    return [arg for key in ('HTTP_PROXY', 'HTTPS_PROXY') for arg in ('--build-arg', f'{key}={proxy}')]


def build(repo: Path, release_sha: str, state_path: Path) -> dict:
    validate_sha(release_sha)
    # Check the state boundary before any source, Docker, or registry action.
    state_parent = state_path.parent
    if state_parent.is_symlink() or not state_parent.is_dir() or state_parent.stat().st_uid != os.getuid() or state_parent.stat().st_mode & 0o077:
        raise ImageWorkerReleaseError('state directory must be owner-only and not symlinked')
    tag = f'{REPOSITORY}:{release_sha}'
    tree = run(['git', '-C', str(repo), 'rev-parse', 'HEAD^{tree}']).strip()
    proxy_args = build_proxy_args()
    run(['docker', 'buildx', 'build', '--platform', 'linux/amd64', '--load', *proxy_args,
         '--label', f'org.opencontainers.image.revision={release_sha}',
         '--label', f'aiops.source-tree={tree}', '-t', tag, '-f',
         str(repo / 'scripts/image_sync/Dockerfile'), str(repo)], timeout=1800)
    info = json.loads(run(['docker', 'image', 'inspect', tag]))[0]
    if (info['Os'], info['Architecture']) != ('linux', 'amd64') or info['Config']['Labels'].get('org.opencontainers.image.revision') != release_sha:
        raise ImageWorkerReleaseError('built worker platform or revision mismatch')
    username, token = tke_candidate._tcr_login_token(runner=None)
    with tke_candidate._temporary_docker_environment() as environment:
        for command, input_bytes, timeout in (
            (['docker', 'login', '--username', username, '--password-stdin', tke_candidate.REGISTRY_HOST], token, 30),
            (['docker', 'push', tag], None, 1800),
        ):
            result = tke_candidate._execute(None, command, input_bytes=input_bytes, timeout_seconds=timeout, environment=environment)
            tke_candidate._require_success(result, 'worker registry operation')
        digest = tke_candidate._remote_digest(remote_tag=tag, component='worker', runner=None, environment=environment)
        image = validate_image(f'{REPOSITORY}@{digest}')
        result = tke_candidate._execute(None, ['docker', 'pull', '--platform', 'linux/amd64', image], timeout_seconds=1800, environment=environment)
        tke_candidate._require_success(result, 'worker immutable pull')
    record = {**preview(release_sha), 'sourceTreeSha': tree, 'image': image, 'status': 'built'}
    write_state(state_path, record)
    return record


def provision_secret() -> dict:
    """Create only a new dedicated Secret; never rotate or overwrite an existing one."""
    # Listing metadata distinguishes NotFound from a transport failure and does
    # not retrieve credential values.
    found = json.loads(kubectl('get', 'secret', NAME, '--ignore-not-found', '-o', 'json') or '{}')
    if found:
        raise ImageWorkerReleaseError('worker Secret already exists; refusing to overwrite')
    payload = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': NAME, 'namespace': NAMESPACE},
               'type': 'Opaque', 'data': {'bridge-token': base64.b64encode(secrets.token_urlsafe(48).encode()).decode()}}
    kubectl('create', '-f', '-', input_text=json.dumps(payload), sensitive=True)
    return {'status': 'secret_created', 'resource': f'Secret/{NAME}'}


def deploy(repo: Path, state_path: Path, release_sha: str, probe_url: str) -> dict:
    record = read_state(state_path, release_sha)
    validate_url(probe_url)
    key_present = kubectl('get', 'secret', NAME, '-o', 'go-template={{if index .data "bridge-token"}}present{{end}}').strip()
    if key_present != 'present':
        raise ImageWorkerReleaseError('dedicated bridge-token Secret is absent')
    documents = render(repo, record['image'], release_sha, probe_url)
    payload = manifest(documents)
    kubectl('apply', '--server-side', '--dry-run=server', '--field-manager=aiops-image-worker', '-f', '-', input_text=payload)
    previous = json.loads(kubectl('get', 'deploy,svc,networkpolicy', '-l', f'app.kubernetes.io/name={NAME}', '-o', 'json'))['items']
    if previous and {f"{d['kind']}/{d['metadata']['name']}" for d in previous} != set(RESOURCES):
        raise ImageWorkerReleaseError('previous worker resource set is incomplete')
    if not record.get('deployAttempted'):
        record.update(previousResources=previous, deployAttempted=True, probeUrl=probe_url)
        write_state(state_path, record)
    kubectl('apply', '--server-side', '--field-manager=aiops-image-worker', '-f', '-', input_text=payload)
    kubectl('rollout', 'status', f'deployment/{NAME}', '--timeout=180s', timeout=210)
    record['status'] = 'deployed'
    write_state(state_path, record)
    return {k: v for k, v in record.items() if k != 'previousResources'}


def verify(state_path: Path, release_sha: str, urls: list[str]) -> dict:
    record = read_state(state_path, release_sha)
    urls = list(dict.fromkeys(validate_url(url) for url in urls))
    if len(urls) < 10:
        raise ImageWorkerReleaseError('verification requires at least ten distinct real image URLs')
    deployment = json.loads(kubectl('get', 'deployment', NAME, '-o', 'json'))
    spec, status = deployment['spec'], deployment.get('status', {})
    if deployment['metadata']['annotations'].get('aiops/release-sha') != release_sha or spec['template']['spec']['containers'][0]['image'] != record['image'] or status.get('observedGeneration', 0) < deployment['metadata']['generation'] or status.get('readyReplicas') != 1:
        raise ImageWorkerReleaseError('live Deployment differs from candidate or is not ready')
    pods = json.loads(kubectl('get', 'pods', '-l', f'app.kubernetes.io/name={NAME}', '-o', 'json'))['items']
    running = [p for p in pods if not p['metadata'].get('deletionTimestamp') and p['status'].get('phase') == 'Running']
    if len(running) != 1:
        raise ImageWorkerReleaseError('expected exactly one running worker Pod')
    pod = running[0]
    cs = pod['status'].get('containerStatuses', [])
    if len(cs) != 1 or not cs[0].get('ready') or record['image'].split('@')[1] not in cs[0].get('imageID', ''):
        raise ImageWorkerReleaseError('live worker imageID or readiness mismatch')
    # Token is read only inside the Pod. The command and output carry no token.
    script = r'''
const urls = JSON.parse(await new Promise(r => { let s=''; process.stdin.on('data',d=>s+=d); process.stdin.on('end',()=>r(s)); }));
const base='http://127.0.0.1:8790';
const headers={Authorization:'Bearer '+process.env.QG_BROWSER_BRIDGE_TOKEN,'Content-Type':'application/json'};
const health=await fetch(base+'/health');
const unauth=await fetch(base+'/fetch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:urls[0]})});
if(health.status!==200 || unauth.status!==401) throw Error('health/auth gate failed');
const probe=await fetch(base+'/probe',{headers});
if(probe.status!==200) throw Error('source probe failed: '+probe.status);
const results=[];
for(const url of urls){
 const response=await fetch(base+'/fetch',{method:'POST',headers,body:JSON.stringify({url}),signal:AbortSignal.timeout(90000)});
 const bytes=Buffer.from(await response.arrayBuffer());
 const type=response.headers.get('content-type') || '';
 const result={url,status:response.status,contentType:type,bytes:bytes.length};
 results.push(result);
 if(response.status!==200 || !type.startsWith('image/') || bytes.length===0){console.log(JSON.stringify({results}));process.exit(1);}
}
console.log(JSON.stringify({health:health.status,unauth:unauth.status,probe:probe.status,results}));
'''
    evidence = json.loads(kubectl('exec', '-i', pod['metadata']['name'], '--', 'node', '--input-type=module', '-e', script, input_text=json.dumps(urls), timeout=1200))
    record.update(status='source_verified', sourceEvidence=evidence, pod=pod['metadata']['name'], imageID=cs[0]['imageID'])
    write_state(state_path, record)
    return {k: v for k, v in record.items() if k != 'previousResources'}


def rollback(state_path: Path, release_sha: str) -> dict:
    record = read_state(state_path, release_sha)
    live = json.loads(kubectl('get', 'deployment', NAME, '-o', 'json'))
    if live['metadata'].get('annotations', {}).get('aiops/release-sha') != release_sha or live['spec']['template']['spec']['containers'][0]['image'] != record['image']:
        raise ImageWorkerReleaseError('worker drift prevents rollback')
    previous = record.get('previousResources')
    if previous is None:
        raise ImageWorkerReleaseError('rollback snapshot is absent')
    if previous:
        for doc in previous:
            for field in ('managedFields', 'resourceVersion', 'uid', 'creationTimestamp', 'generation'):
                doc['metadata'].pop(field, None)
            doc.pop('status', None)
        kubectl('apply', '--server-side', '--field-manager=aiops-image-worker', '-f', '-', input_text=manifest(previous))
        kubectl('rollout', 'status', f'deployment/{NAME}', '--timeout=180s', timeout=210)
    else:
        # First install has no predecessor: disable instead of deleting data or
        # a credential. No main application resource is involved.
        kubectl('scale', f'deployment/{NAME}', '--replicas=0')
    record['status'] = 'rolled_back'
    write_state(state_path, record)
    return {'status': 'rolled_back', 'releaseSha': release_sha, 'previousRestored': bool(previous)}
