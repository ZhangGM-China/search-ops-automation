"""Stable graph preconditions and identity checks for generated KG actions."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

SYNONYM = '902227138815852544'
SUBORDINATE = '902227092548485120'
READS = {'query_tag', 'query_relationship', 'query_dimension'}
_VOLATILE = {'createTime', 'updateTime', 'created_at', 'updated_at', 'timestamp', 'traceId', 'requestId'}


def canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): canonical(v) for k, v in sorted(value.items()) if k not in _VOLATILE}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    return value


def graph_result(result: Mapping[str, Any]) -> dict[str, Any]:
    if result.get('error') or result.get('complete') is False:
        raise ValueError('图谱查询失败或未读全')
    rows = result.get('data')
    if not isinstance(rows, list):
        raise ValueError('图谱查询缺少列表证据')
    return {'data': sorted((canonical(row) for row in rows), key=lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False))}


def graph_context(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    reads = []
    for call in calls:
        if call.get('name') in READS:
            reads.append({'name': call['name'], 'args': dict(call['args']), 'result': graph_result(call['result'])})
    # Last read for a query wins, so repeated investigations do not duplicate evidence.
    indexed = {json.dumps([r['name'], r['args']], sort_keys=True): r for r in reads}
    return {'version': 2, 'reads': [indexed[k] for k in sorted(indexed)]}


def digest(context: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _id(value: Any) -> str:
    return str(value or '').removeprefix('kg_tag/')


def validate_actions(actions: Sequence[Mapping[str, Any]], context: Mapping[str, Any], scene_id: str = '') -> None:
    tags: dict[str, Mapping[str, Any]] = {}
    relationships: dict[str, Mapping[str, Any]] = {}
    dimensions: set[str] = set()
    queried_tags: set[str] = set()
    source_reads: set[str] = set()
    scenes: set[str] = {scene_id} if scene_id else set()
    for read in context.get('reads', []):
        args = read['args']
        rows = read['result']['data']
        if read['name'] == 'query_tag':
            queried_tags.add(args['tag_name'])
            for row in rows:
                tags[_id(row.get('id'))] = row
                scenes.add(str(row.get('sceneId') or row.get('scene_id') or ''))
                dimensions.update(str(v) for v in row.get('dimensions', []) if v)
        elif read['name'] == 'query_relationship':
            if args.get('side', 'both') in {'source', 'both'}:
                source_reads.add(args['tag_name'])
            for row in rows:
                relationships[str(row.get('id'))] = row
        elif read['name'] == 'query_dimension':
            dimensions.update(str(row.get('id')) for row in rows if row.get('id'))
    scenes.discard('')
    created: dict[str, Mapping[str, Any]] = {}

    def get_tag(identity, name=None):
        row = created.get(str(identity)) or tags.get(_id(identity))
        if not row or (name and (row.get('name') or row.get('tagName')) != name):
            raise ValueError('标签身份未经查询确认')
        return row

    for step, action in enumerate(actions, 1):
        kind, params = action['actionType'], action['params']
        if kind != 'delete_relationship' and str(params.get('sceneId')) not in scenes:
            raise ValueError('场景 ID 未经确认')
        if kind in {'update_relationship', 'delete_relationship'}:
            existing = relationships.get(str(params.get('id')))
            if not existing:
                raise ValueError('关系 ID 未经查询确认')
            if kind == 'update_relationship' and (existing.get('tagName') != params.get('tagName') or existing.get('responseTagName') != params.get('responseTagName')):
                raise ValueError('更新关系不能变更端点，应删除后新建')
            if kind == 'delete_relationship':
                relationships.pop(str(params['id']))
                continue
        if kind in {'add_tag', 'update_tag'}:
            if str(params['dimensions']) not in dimensions:
                raise ValueError('维度 ID 未经查询确认')
            if kind == 'add_tag':
                if params['name'] not in queried_tags:
                    raise ValueError('新标签必须先精确查重')
                if any((r.get('name') or r.get('tagName')) == params['name'] for r in tags.values()):
                    raise ValueError('标签已存在')
                created[f'@step:{step}'] = params
            else:
                row = get_tag(params['id'])
                if str(row.get('isRoot')) != str(params['isRoot']):
                    raise ValueError('根标签角色变更需要单独核验同义和从属关系')
            homophones = str(params.get('homophonic') or '').replace('，', ',').split(',')
            for word in filter(None, (w.strip() for w in homophones)):
                if word not in queried_tags or any((r.get('name') or r.get('tagName')) == word for r in tags.values()):
                    raise ValueError('谐音词必须查明确认不是已有标签')
        elif kind == 'blacklist_tag':
            row = get_tag(params['id'])
            if str(row.get('tagId')) != str(params['tagId']):
                raise ValueError('标签业务 ID 不匹配')
        elif kind in {'add_relationship', 'update_relationship'}:
            source = get_tag(params['tagId'], params['tagName'])
            target = get_tag(params['responseTagId'], params['responseTagName'])
            relation = str(params['relationships'])
            if relation == SYNONYM:
                if str(source.get('isRoot')) != '0' or str(target.get('isRoot')) != '1':
                    raise ValueError('同义关系必须是非根到根')
            elif str(source.get('isRoot')) != '1' or str(target.get('isRoot')) != '1':
                raise ValueError('从属关系必须是根到根')
            if params['tagName'] not in source_reads and not str(params['tagId']).startswith('@step:'):
                raise ValueError('必须查询起点已有关系')
            for rid, row in relationships.items():
                if rid == str(params.get('id')):
                    continue
                if row.get('tagName') == params['tagName'] and SYNONYM in [str(v) for v in row.get('relationships', [])]:
                    if relation == SYNONYM and row.get('responseTagName') != params['responseTagName']:
                        raise ValueError('非根标签不能同义于多个根')
                if row.get('tagName') == params['tagName'] and row.get('responseTagName') == params['responseTagName'] and relation in [str(v) for v in row.get('relationships', [])]:
                    raise ValueError('关系已存在')
            if relation == SUBORDINATE:
                pending = [params['responseTagName']]
                visited = set()
                while pending:
                    name = pending.pop()
                    if name == params['tagName']:
                        raise ValueError('从属关系成环')
                    if name in visited:
                        continue
                    visited.add(name)
                    if name not in source_reads and not any(r['name'] == name for r in created.values()):
                        raise ValueError('需查询父标签祖先关系以排除环')
                    pending.extend(row['responseTagName'] for rid, row in relationships.items() if rid != str(params.get('id')) and row.get('tagName') == name and SUBORDINATE in [str(v) for v in row.get('relationships', [])])
            relationships[str(params.get('id') or f'new:{step}')] = {**params, 'relationships': [relation]}
