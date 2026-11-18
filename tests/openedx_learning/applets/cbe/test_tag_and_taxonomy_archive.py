"""
Tests that a learner's competency status still resolves after its tag, or its taxonomy, is deleted.

These live here rather than in ``tests/openedx_tagging`` because they need a StudentCompetencyStatus, and the
tagging package's tests shouldn't depend on this one.
"""
from datetime import datetime

import pytest

from openedx_learning.models import CompetencyTaxonomy, MasteryStatus, StudentCompetencyStatus
from openedx_tagging import api as tagging_api
from openedx_tagging.models import ObjectTag, Tag

pytestmark = pytest.mark.django_db


@pytest.fixture(name="learner_status")
def _learner_status(user, tag: Tag, object_tag: ObjectTag, now: datetime) -> StudentCompetencyStatus:
    """A learner's status for `tag`, with the association it was evaluated through locked against deletion."""
    tagging_api.lock_object_tags_for_deletion(object_tag.pk)
    return StudentCompetencyStatus.objects.create(
        user=user, tag=tag, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )


def test_status_resolves_through_archived_tag(
    learner_status: StudentCompetencyStatus, competency_taxonomy: CompetencyTaxonomy, tag: Tag,
) -> None:
    """
    AC: deleting a tag that learner mastery depends on archives it, and the status still resolves through it.
    """
    tagging_api.delete_tags_from_taxonomy(competency_taxonomy, [tag.value], with_subtags=False)

    resolved = StudentCompetencyStatus.objects.get(pk=learner_status.pk).tag
    assert resolved.pk == tag.pk
    assert resolved.archived is True


def test_status_resolves_through_archived_taxonomy(
    learner_status: StudentCompetencyStatus, competency_taxonomy: CompetencyTaxonomy, tag: Tag,
) -> None:
    """
    AC: deleting a taxonomy that learner mastery depends on archives it, and the status still resolves through it.
    """
    tagging_api.delete_taxonomy(competency_taxonomy)

    resolved = StudentCompetencyStatus.objects.get(pk=learner_status.pk).tag
    assert resolved.pk == tag.pk
    assert resolved.archived is False
    assert resolved.taxonomy is not None
    assert resolved.taxonomy.archived is True
