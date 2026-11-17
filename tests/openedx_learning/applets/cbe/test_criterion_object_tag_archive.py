"""
Tests that archiving a locked ObjectTag, instead of deleting it, keeps the criteria that point at it.

These live here rather than in ``tests/openedx_tagging`` because they need a CompetencyCriterion, and the
tagging package's tests shouldn't depend on this one.
"""
from datetime import datetime

import pytest

from openedx_learning.models import (
    CompetencyCriteriaGroup,
    CompetencyCriterion,
    CompetencyRuleProfile,
    CompetencyTaxonomy,
    MasteryStatus,
    StudentCompetencyCriterionStatus,
)
from openedx_tagging import api as tagging_api
from openedx_tagging.models import ObjectTag, Tag

pytestmark = pytest.mark.django_db


def test_criterion_survives_remove_and_reapply(
    *,
    group: CompetencyCriteriaGroup,
    object_tag: ObjectTag,
    default_rule_profile: CompetencyRuleProfile,
    competency_taxonomy: CompetencyTaxonomy,
    tag: Tag,
    user,
    now: datetime,
) -> None:
    """
    AC: removing a locked object tag archives it, so its criterion and a learner's status for that criterion
    survive; applying the tag again restores the object tag.
    """
    tagging_api.lock_object_tags_for_deletion(object_tag.pk)
    criterion = CompetencyCriterion.objects.create(
        group=group, object_tag=object_tag, rule_profile=default_rule_profile
    )
    learner_status = StudentCompetencyCriterionStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )

    tagging_api.tag_object(object_tag.object_id, competency_taxonomy, [])

    archived = ObjectTag.objects.get(pk=object_tag.pk)
    assert archived.archived is True
    assert archived.deletion_locked is True
    assert CompetencyCriterion.objects.get(pk=criterion.pk).object_tag == archived
    assert StudentCompetencyCriterionStatus.objects.get(pk=learner_status.pk).criterion_id == criterion.pk

    tagging_api.tag_object(object_tag.object_id, competency_taxonomy, [tag.value])

    reapplied = ObjectTag.objects.get(object_id=object_tag.object_id, taxonomy=competency_taxonomy)
    assert reapplied.pk == object_tag.pk
    assert reapplied.archived is False
    assert CompetencyCriterion.objects.get(pk=criterion.pk).object_tag_id == object_tag.pk
    assert StudentCompetencyCriterionStatus.objects.get(pk=learner_status.pk).criterion_id == criterion.pk
