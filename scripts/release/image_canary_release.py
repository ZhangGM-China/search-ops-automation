"""Bounded image canary using the accepted live Backend image, not a main release."""
import json
import os
import re
from pathlib import Path

from scripts.release import image_worker_release as worker

ASSETS_SECRET_NAME='qg-image-assets'
COS_KEYS=('QUERY_GOVERNANCE_COS_REGION','QUERY_GOVERNANCE_COS_BUCKET','QUERY_GOVERNANCE_COS_PUBLIC_BASE_URL',
          'QUERY_GOVERNANCE_COS_KEY_PREFIX','TENCENT_COS_SECRET_ID','TENCENT_COS_SECRET_KEY')
STATE_FILE='image-canary-state.json'


def job_name(sha):
    return 'qg-image-canary-'+worker.validate_sha(sha)[:12]


def provision_assets(envfile):
    from dotenv import dotenv_values
    existing=json.loads(worker.kubectl('get','secret',ASSETS_SECRET_NAME,'--ignore-not-found','-o','json') or '{}')
    if existing:
        raise worker.ImageWorkerReleaseError('image assets Secret exists; refusing to overwrite')
    if envfile.is_symlink() or envfile.stat().st_uid!=os.getuid() or envfile.stat().st_mode & 0o077:
        raise worker.ImageWorkerReleaseError('runtime env file must be owner-only')
    values=dotenv_values(envfile)
    if any(not values.get(k) for k in COS_KEYS):
        raise worker.ImageWorkerReleaseError('image assets configuration is incomplete')
    if not values['QUERY_GOVERNANCE_COS_KEY_PREFIX'].startswith('query-governance/images/prod/'):
        raise worker.ImageWorkerReleaseError('image assets prefix must use production namespace')
    doc={'apiVersion':'v1','kind':'Secret','metadata':{'name':ASSETS_SECRET_NAME,'namespace':worker.NAMESPACE},
         'type':'Opaque','stringData':{k:values[k] for k in COS_KEYS}}
    worker.kubectl('create','-f','-',input_text=json.dumps(doc),sensitive=True)
    return {'status':'secret_created','resource':'Secret/'+ASSETS_SECRET_NAME,'keys':list(COS_KEYS)}


def render(repo,image,sha):
    worker.validate_sha(sha)
    if not re.fullmatch(r'registry\.example\.test/aiops/backend@sha256:[0-9a-f]{64}',image):
        raise worker.ImageWorkerReleaseError('canary requires immutable accepted Backend image')
    name=job_name(sha)
    labels={'app.kubernetes.io/name':'qg-image-sync','aiops/image-canary':name}
    env=[{'name':'ENV','value':'prod'}, {'name':'QG_BROWSER_BRIDGE_URL','value':'http://qg-atlas-browser-worker:8790'},
         {'name':'QG_BROWSER_BRIDGE_TOKEN','valueFrom':{'secretKeyRef':{'name':worker.NAME,'key':'bridge-token'}}},
         {'name':'AUTO_DB_URI_PROD','valueFrom':{'secretKeyRef':{'name':'aiops-runtime-secrets','key':'AUTO_DB_URI_PROD'}}},
         {'name':'ATLAS_API_KEY','valueFrom':{'secretKeyRef':{'name':'aiops-runtime-secrets','key':'ATLAS_API_KEY'}}}]
    env += [{'name':k,'valueFrom':{'secretKeyRef':{'name':ASSETS_SECRET_NAME,'key':k}}} for k in COS_KEYS]
    # Read-only runtime configuration contains brand endpoint/auth locations, not copied credential values.
    pod={'restartPolicy':'Never','automountServiceAccountToken':False,'imagePullSecrets':[{'name':'aiops-tcr-pull'}],
         'securityContext':{'runAsNonRoot':True,'runAsUser':10001,'runAsGroup':10001,'seccompProfile':{'type':'RuntimeDefault'}},
         'containers':[{'name':'canary','image':image,'command':['python','-c',(repo/'scripts/image_sync/online_canary.py').read_text()],
                        'env':env,'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'1','memory':'768Mi'}},
                        'securityContext':{'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}},
                        'volumeMounts':[{'name':'config','mountPath':'/app/config.yaml','subPath':'config.yaml','readOnly':True}]}],
         'volumes':[{'name':'config','configMap':{'name':'aiops-runtime-config'}}]}
    return [{'apiVersion':'batch/v1','kind':'Job','metadata':{'name':name,'namespace':worker.NAMESPACE,'labels':labels},
             'spec':{'backoffLimit':0,'activeDeadlineSeconds':600,'ttlSecondsAfterFinished':86400,
                     'template':{'metadata':{'labels':labels},'spec':pod}}},
            {'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy',
             'metadata':{'name':name,'namespace':worker.NAMESPACE,'labels':labels},
             'spec':{'podSelector':{'matchLabels':{'aiops/image-canary':name}},'policyTypes':['Egress'],
                     'egress':[{'ports':[{'protocol':'TCP','port':443},{'protocol':'TCP','port':3306},
                                        {'protocol':'UDP','port':53},{'protocol':'TCP','port':53}]},
                               {'to':[{'podSelector':{'matchLabels':{'app.kubernetes.io/name':worker.NAME}}}],
                                'ports':[{'protocol':'TCP','port':8790}]}]}}]


def deploy(repo,state_path,sha,worker_sha,worker_state_path,expected_backend_image):
    live=worker.read_state(worker_state_path,worker_sha)
    if live.get('status')!='source_verified':
        raise worker.ImageWorkerReleaseError('source verification is required')
    dep=json.loads(worker.kubectl('get','deploy',worker.NAME,'-o','json'))
    if dep['spec']['template']['spec']['containers'][0]['image']!=live['image'] or dep.get('status',{}).get('readyReplicas')!=1:
        raise worker.ImageWorkerReleaseError('live Worker drift')
    service=json.loads(worker.kubectl('get','svc','api','-o','json'))
    slot=service['spec']['selector'].get('aiops.example.test/slot')
    if slot not in ('blue','green'):
        raise worker.ImageWorkerReleaseError('active application slot is unknown')
    backend=json.loads(worker.kubectl('get','deploy','backend-'+slot,'-o','json'))
    image=backend['spec']['template']['spec']['containers'][0]['image']
    if image!=expected_backend_image:
        raise worker.ImageWorkerReleaseError('active Backend image drift')
    docs=render(repo,image,sha)
    payload=worker.manifest(docs)
    worker.kubectl('apply','--server-side','--dry-run=server','--field-manager=aiops-image-canary','-f','-',input_text=payload)
    worker.write_state(state_path,{'releaseSha':sha,'workerReleaseSha':worker_sha,'status':'deploying','job':job_name(sha),'backendImage':image})
    # Create, not apply: never overwrite an existing immutable Job or rerun a canary silently.
    worker.kubectl('create','-f','-',input_text=payload)
    return verify(state_path,sha,wait=True)


def verify(state_path,sha,wait=False):
    record=json.loads(state_path.read_text())
    if record['releaseSha']!=worker.validate_sha(sha) or record['job']!=job_name(sha):
        raise worker.ImageWorkerReleaseError('canary identity drift')
    if wait:
        worker.kubectl('wait','--for=condition=complete','job/'+record['job'],'--timeout=600s',timeout=630)
    job=json.loads(worker.kubectl('get','job',record['job'],'-o','json'))
    if job.get('status',{}).get('succeeded')!=1:
        raise worker.ImageWorkerReleaseError('canary Job did not succeed')
    logs=worker.kubectl('logs','job/'+record['job'],sensitive=True)
    line=next((line[len('IMAGE_CANARY_RESULT='):] for line in logs.splitlines() if line.startswith('IMAGE_CANARY_RESULT=')),None)
    result=json.loads(line or '{}')
    products=result.get('products',[])
    if result.get('status')!='accepted' or len(products)!=10 or len({p['productId'] for p in products})!=10 or not all(all(p.get(k) is True for k in ('mappingReady','cosHead','publicImage','cacheReuse')) for p in products):
        raise worker.ImageWorkerReleaseError('COS/Mapping/public-image/reuse proof is incomplete')
    record.update(status='accepted',evidence=result)
    worker.write_state(state_path,record)
    return record
