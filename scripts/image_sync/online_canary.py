"""Small production canary: live on-sale catalog, browser, COS and Mapping.

Executed only by the scoped release Job; no cron and no full catalog import.
"""
import json
import os
from urllib.parse import urlsplit

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.integrations.operational_service import OperationalService
from src.query_governance.product_adapter import ProductAdapterRegistry
from src.query_governance.relevance import (
    ImageCacheService, PillowImageTransformer, RelevanceError,
    SqlAlchemyImageAssetStore, TencentCosObjectStorage,
)


class BrowserFetcher:
    def __init__(self):
        self.calls = 0

    def fetch(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'static.atlas.example.test' or parsed.username or parsed.password or parsed.port not in (None,443):
            raise RelevanceError('IMAGE_URL_INVALID','Invalid canary source')
        self.calls += 1
        with requests.post(os.environ['QG_BROWSER_BRIDGE_URL']+'/fetch', json={'url':url},
                           headers={'Authorization':'Bearer '+os.environ['QG_BROWSER_BRIDGE_TOKEN']}, timeout=90, stream=True) as response:
            if response.status_code != 200:
                raise RelevanceError('IMAGE_BROWSER_FAILED',f'Worker HTTP {response.status_code}')
            chunks=[]
            size=0
            for chunk in response.iter_content(65536):
                size+=len(chunk)
                if size>8*1024*1024:
                    raise RelevanceError('IMAGE_TOO_LARGE','Worker body limit')
                chunks.append(chunk)
            content_type=response.headers.get('content-type','').split(';',1)[0]
            payload=b''.join(chunks)
            if not payload or not content_type.startswith('image/'):
                raise RelevanceError('IMAGE_UNSUPPORTED_MEDIA','Invalid Worker response')
            return payload,content_type


STAGE="initialize"


def main():
    global STAGE
    if os.getenv('ENV')!='prod':
        raise RuntimeError('Canary requires explicit production mode')
    engine=create_engine(os.environ['AUTO_DB_URI_PROD'],pool_pre_ping=True,connect_args={'connect_timeout':10})
    store=SqlAlchemyImageAssetStore(sessionmaker(bind=engine))
    storage=TencentCosObjectStorage()
    fetcher=BrowserFetcher()
    cache=ImageCacheService(fetcher=fetcher,transformer=PillowImageTransformer(),storage=storage,
                            asset_store=store,key_prefix=os.environ['QUERY_GOVERNANCE_COS_KEY_PREFIX'],max_workers=1)
    STAGE="catalog"
    operational=OperationalService()
    adapter=ProductAdapterRegistry()
    products=[]
    ids=set()
    # Limit discovery to five pages; never quietly turn acceptance into all-catalog synchronization.
    for page in range(1,6):
        response=operational._call_api('POST', operational._get_base_url('atlas')+'/admin-api/search/product/list',
                    'atlas','image_canary',json={'status':1,'pageNum':page,'pageSize':100},
                    headers=operational._get_headers('atlas'),timeout=30)
        if response.status_code!=200:
            raise RuntimeError('Live catalog HTTP failure')
        body=response.json()
        if body.get('code') not in (None,0,200):
            raise RuntimeError('Live catalog business failure')
        rows=(body.get('data') or {}).get('list') or (body.get('data') or {}).get('rows') or []
        for row in rows:
            if str(row.get('status'))!='1':
                continue
            try:
                product=adapter.adapt('atlas',row,len(products)+1)
            except ValueError:
                continue
            if product.product_id in ids or not product.image_url or urlsplit(product.image_url).hostname!='static.atlas.example.test':
                continue
            products.append(product)
            ids.add(product.product_id)
            if len(products)==10:
                break
        if len(products)==10 or not rows:
            break
    print("IMAGE_CANARY_DISCOVERY="+json.dumps({"eligible":len(products)}),flush=True)
    if len(products)!=10:
        raise RuntimeError('Canary needs ten distinct currently on-sale Atlas products')
    results=[]
    for product in products:
        STAGE="prepare_cos_mapping"
        before=store.get('atlas',product.product_id,product.spu_id)
        asset=cache.prepare_products([product])[product.image_url]
        if not asset.available or not asset.url:
            raise RuntimeError('COS preparation failed: '+str(asset.warning_code))
        record=store.get('atlas',product.product_id,product.spu_id)
        if record is None or record.status!='ready' or record.cached_url!=asset.url or not storage.exists(record.storage_key):
            raise RuntimeError('COS HEAD or Mapping readback failed')
        # Public URL is what the AI receives, so authenticated SDK HEAD alone is insufficient.
        STAGE="public_cos_access"
        with requests.get(record.cached_url,stream=True,timeout=30) as response:
            if response.status_code!=200 or not response.headers.get('content-type','').startswith('image/'):
                raise RuntimeError('Public COS image inaccessible')
            body=next(response.iter_content(65536),b'')
            if not body:
                raise RuntimeError('Public COS image empty')
        STAGE="cache_reuse"
        count=fetcher.calls
        again=cache.prepare_products([product])[product.image_url]
        if not again.available or again.url!=asset.url or fetcher.calls!=count:
            raise RuntimeError('COS-first reuse failed')
        results.append({'productId':product.product_id,'mappingReady':True,'cosHead':True,'publicImage':True,
                        'cacheReuse':True,'previousMapping':bool(before and before.status=='ready')})
    print('IMAGE_CANARY_RESULT='+json.dumps({'status':'accepted','products':results,'browserFetches':fetcher.calls}),flush=True)


if __name__=='__main__':
    try:
        main()
    except Exception as error:
        # Never emit SDK/auth/db exception strings, which may contain credentials.
        print('IMAGE_CANARY_ERROR='+STAGE+':'+type(error).__name__,flush=True)
        raise SystemExit(1)
