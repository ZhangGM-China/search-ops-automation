"""One strict schema and validator for error-product KG proposals.

The recall Agent keeps its existing contract. No business defaults are added.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strict=True, min_length=1)]
Flag = Literal['0', '1']


class ContractModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class AddTagParams(ContractModel):
    name: Text
    viewTag: Text
    sceneId: Text
    dimensions: Text
    isEnglish: Annotated[StrictInt, Field(ge=0, le=1)]
    isRoot: Flag
    segmentingAgain: Flag
    isCompose: Flag
    weight: StrictInt
    nameAnd: list[Text]
    nameNot: list[Text]
    homophonic: str | None = None

    @model_validator(mode='after')
    def validate_composition(self):
        if self.isCompose == '1' and not self.nameAnd:
            raise ValueError('composed tag requires nonempty nameAnd')
        return self


class UpdateTagParams(AddTagParams):
    id: Text


class BlacklistTagParams(ContractModel):
    id: Text
    tagId: Text
    sceneId: Text
    isBlack: Annotated[StrictInt, Field(ge=1, le=1)]


class AddRelationshipParams(ContractModel):
    sceneId: Text
    relationships: Literal['902227138815852544', '902227092548485120']
    tagId: Text
    tagName: Text
    dimensionNames: Text
    responseTagId: Text
    responseTagName: Text


class UpdateRelationshipParams(AddRelationshipParams):
    id: Text


class DeleteRelationshipParams(ContractModel):
    id: Text


class Action(ContractModel):
    reason: str
    dependsOn: list[Annotated[StrictInt, Field(ge=1)]] = Field(default_factory=list)


class AddTag(Action):
    actionType: Literal['add_tag']
    params: AddTagParams


class UpdateTag(Action):
    actionType: Literal['update_tag']
    params: UpdateTagParams


class BlacklistTag(Action):
    actionType: Literal['blacklist_tag']
    params: BlacklistTagParams


class AddRelationship(Action):
    actionType: Literal['add_relationship']
    params: AddRelationshipParams


class UpdateRelationship(Action):
    actionType: Literal['update_relationship']
    params: UpdateRelationshipParams


class DeleteRelationship(Action):
    actionType: Literal['delete_relationship']
    params: DeleteRelationshipParams


KGAction = Annotated[AddTag | UpdateTag | BlacklistTag | AddRelationship | UpdateRelationship | DeleteRelationship, Field(discriminator='actionType')]


class StrategyGroup(ContractModel):
    groupId: Text
    rootCause: Literal['individual', 'kg_issue', 'uncertain']
    causeKey: Text
    reason: str
    productIds: Annotated[list[Text], Field(min_length=1)]
    confidence: Annotated[float, Field(ge=0, le=1)]
    actions: list[KGAction]

    @model_validator(mode='after')
    def validate_group(self):
        if (self.rootCause == 'kg_issue') != bool(self.actions):
            raise ValueError('kg_issue must have actions; individual/uncertain must have none')
        if len(set(self.productIds)) != len(self.productIds):
            raise ValueError('product cannot repeat in one group')
        for step, action in enumerate(self.actions, 1):
            if len(set(action.dependsOn)) != len(action.dependsOn) or any(dep >= step for dep in action.dependsOn):
                raise ValueError('dependsOn must uniquely reference earlier actions')
            for key, value in action.params.model_dump(exclude_unset=True).items():
                if not isinstance(value, str) or not value.startswith('@step:'):
                    continue
                match = re.fullmatch(r'@step:([1-9][0-9]*)', value)
                if key not in {'tagId', 'responseTagId'} or action.actionType not in {'add_relationship', 'update_relationship'} or not match:
                    raise ValueError('step reference is only allowed for relationship endpoints')
                dep = int(match.group(1))
                if dep >= step or dep not in action.dependsOn or self.actions[dep - 1].actionType != 'add_tag':
                    raise ValueError('step reference must depend on an earlier add_tag action')
        return self


class RelevanceKGOutput(ContractModel):
    groups: list[StrategyGroup]


RELEVANCE_KG_OUTPUT_SCHEMA = RelevanceKGOutput.model_json_schema()
# Cross-field conditions complement Pydantic's runtime validators.
RELEVANCE_KG_OUTPUT_SCHEMA['$defs']['StrategyGroup']['allOf'] = [
    {'if': {'properties': {'rootCause': {'const': 'kg_issue'}}},
     'then': {'properties': {'actions': {'minItems': 1}}},
     'else': {'properties': {'actions': {'maxItems': 0}}}},
]
for _name in ('AddTagParams', 'UpdateTagParams'):
    RELEVANCE_KG_OUTPUT_SCHEMA['$defs'][_name]['allOf'] = [
        {'if': {'properties': {'isCompose': {'const': '1'}}},
         'then': {'properties': {'nameAnd': {'minItems': 1}}}},
    ]


def validate_relevance_output(payload: Any, product_ids: set[str]) -> dict[str, Any]:
    validated = RelevanceKGOutput.model_validate(payload)
    seen: set[str] = set()
    group_ids: set[str] = set()
    for group in validated.groups:
        if group.groupId in group_ids:
            raise ValueError('groupId must be unique')
        group_ids.add(group.groupId)
        if seen.intersection(group.productIds):
            raise ValueError('product must belong to one group')
        seen.update(group.productIds)
    if seen != product_ids:
        raise ValueError('groups must cover every input product exactly once')
    # Validation must not inject optional fields or defaults into model proposals.
    return validated.model_dump(exclude_unset=True)
