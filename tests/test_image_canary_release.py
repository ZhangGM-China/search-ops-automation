import json
from pathlib import Path

import pytest

from scripts.release import image_canary_release as canary
from scripts.release.release import build_parser


def test_canary_entrypoint_is_gated():
    args = build_parser().parse_args(['tke-image-canary', '--phase', 'preview', '--release-sha', 'a'*40, '--worker-release-sha', 'b'*40, '--kube-context', canary.worker.CONTEXT])
    assert args.phase == 'preview'


def test_canary_renders_isolated_job_and_network_policy():
    root = Path(__file__).resolve().parents[1]
    image = 'registry.example.test/aiops/backend@sha256:' + 'd'*64
    docs = canary.render(root, image, 'a'*40)
    assert [x['kind'] for x in docs] == ['Job', 'NetworkPolicy']
    pod = docs[0]['spec']['template']['spec']
    assert pod['automountServiceAccountToken'] is False
    assert docs[0]['spec']['backoffLimit'] == 0
    assert pod['containers'][0]['image'] == image
    text = json.dumps(docs)
    assert 'bridge-token' in text
    assert 'qg-image-assets' in text
    assert 'secretKeyRef' in text
    assert 'backend-blue' not in text


def test_canary_rejects_mutable_or_wrong_repository_images():
    for image in ['backend:latest', 'registry.example.test/other/backend@sha256:'+'c'*64]:
        with pytest.raises(ValueError):
            canary.render(Path('.'), image, 'a'*40)


def test_assets_secret_never_overwrites_existing(monkeypatch, tmp_path):
    monkeypatch.setattr(canary.worker, 'kubectl', lambda *a, **k: '{"metadata":{"name":"qg-image-assets"}}')
    with pytest.raises(ValueError, match='exists'):
        canary.provision_assets(tmp_path/'unused')


def test_assets_secret_keeps_values_out_of_result(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(canary.worker, 'kubectl', lambda *a, **kw: calls.append((a,kw)) or '')
    envfile = tmp_path/'runtime.env'
    envfile.write_text('\n'.join(f'{k}=' + ('query-governance/images/prod/v1' if k == 'QUERY_GOVERNANCE_COS_KEY_PREFIX' else f'test-only-{i}') for i,k in enumerate(canary.COS_KEYS)))
    envfile.chmod(0o600)
    result = canary.provision_assets(envfile)
    doc = json.loads(calls[-1][1]['input_text'])
    assert calls[-1][1]['sensitive'] is True
    assert set(doc['stringData']) == set(canary.COS_KEYS)
    assert 'test-only' not in json.dumps(result)


def test_canary_failure_never_records_acceptance(monkeypatch,tmp_path):
    import base64
    state = tmp_path/'private'/canary.STATE_FILE
    canary.worker.write_state(state, {'releaseSha':'a'*40, 'status':'running', 'job':canary.job_name('a'*40)})
    monkeypatch.setattr(canary.worker, 'kubectl', lambda *a, **kw: json.dumps({'status':{'failed':1}}))
    with pytest.raises(ValueError):
        canary.verify(state, 'a'*40)
    assert json.loads(state.read_text())['status'] != 'accepted'


def _canary_state(tmp_path):
    state=tmp_path/'private'/canary.STATE_FILE
    image='registry.example.test/aiops/backend@sha256:'+'d'*64
    canary.worker.write_state(state,{'releaseSha':'a'*40,'job':canary.job_name('a'*40),'status':'running','backendImage':image})
    return state,image


def test_canary_verifies_all_ten_public_cos_and_reuse_results(monkeypatch,tmp_path):
    state,image=_canary_state(tmp_path)
    products=[{'productId':str(i),'mappingReady':True,'cosHead':True,'publicImage':True,'cacheReuse':True} for i in range(10)]
    result={'status':'accepted','products':products,'browserFetches':10}
    calls=[]
    def command(*a,**k):
        calls.append(a)
        if a[0]=='logs':return 'IMAGE_CANARY_RESULT='+json.dumps(result)
        if a[:2]==('get','job'): return json.dumps({'status':{'succeeded':1}})
        return ''
    monkeypatch.setattr(canary.worker,'kubectl',command)
    assert canary.verify(state,'a'*40,wait=True)['status']=='accepted'
    assert calls[0][0]=='wait'


@pytest.mark.parametrize('kind',['missing','duplicate','public_failure'])
def test_canary_rejects_incomplete_business_evidence(monkeypatch,tmp_path,kind):
    state,image=_canary_state(tmp_path)
    products=[{'productId':str(i),'mappingReady':True,'cosHead':True,'publicImage':True,'cacheReuse':True} for i in range(10)]
    if kind=='missing':products.pop()
    if kind=='duplicate':products[1]['productId']='0'
    if kind=='public_failure':products[0]['publicImage']=False
    def command(*a,**k):
        if a[0]=='logs':return 'IMAGE_CANARY_RESULT='+json.dumps({'status':'accepted','products':products})
        return json.dumps({'status':{'succeeded':1}})
    monkeypatch.setattr(canary.worker,'kubectl',command)
    with pytest.raises(ValueError):canary.verify(state,'a'*40)
    assert json.loads(state.read_text())['status']=='running'


def test_canary_deploy_reads_live_slot_and_never_changes_main_deployments(monkeypatch,tmp_path):
    state,image=_canary_state(tmp_path)
    calls=[]
    def command(*a,**k):
        calls.append(a)
        if a[:3]==('get','deploy',canary.worker.NAME):
            return json.dumps({'spec':{'template':{'spec':{'containers':[{'image':'worker-image'}]}}},'status':{'readyReplicas':1}})
        if a[:3]==('get','svc','api'):
            return json.dumps({'spec':{'selector':{'aiops.example.test/slot':'blue'}}})
        if a[:3]==('get','deploy','backend-blue'):
            return json.dumps({'spec':{'template':{'spec':{'containers':[{'image':image}]}}}})
        return ''
    monkeypatch.setattr(canary.worker,'kubectl',command)
    monkeypatch.setattr(canary.worker,'read_state',lambda *a:{'status':'source_verified','image':'worker-image'})
    monkeypatch.setattr(canary,'verify',lambda *a,**k:{'status':'accepted'})
    result=canary.deploy(Path(__file__).resolve().parents[1],state,'a'*40,'b'*40,Path('ignored'),image)
    assert result['status']=='accepted'
    assert ('create','-f','-') in calls
    assert not any('set' in a or 'patch' in a or 'delete' in a for a in calls)


def test_canary_refuses_active_image_drift_before_create(monkeypatch,tmp_path):
    state,image=_canary_state(tmp_path)
    calls=[]
    def command(*a,**k):
        calls.append(a)
        if a[:3]==('get','deploy',canary.worker.NAME):return json.dumps({'spec':{'template':{'spec':{'containers':[{'image':'worker-image'}]}}},'status':{'readyReplicas':1}})
        if a[:3]==('get','svc','api'):return json.dumps({'spec':{'selector':{'aiops.example.test/slot':'blue'}}})
        return json.dumps({'spec':{'template':{'spec':{'containers':[{'image':image}]}}}})
    monkeypatch.setattr(canary.worker,'kubectl',command)
    monkeypatch.setattr(canary.worker,'read_state',lambda *a:{'status':'source_verified','image':'worker-image'})
    with pytest.raises(ValueError,match='drift'):
        canary.deploy(Path('.'),state,'a'*40,'b'*40,Path('ignored'),'different-image')
    assert not any(a[0] in ('create','apply') for a in calls)
