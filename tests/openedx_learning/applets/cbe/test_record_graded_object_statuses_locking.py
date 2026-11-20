"""
These tests cover the deletion lock that `record_graded_object_statuses` sets on the tag association behind each
evaluated criterion.
"""
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from openedx_learning.api import GradedObjectScore, record_graded_object_statuses
from openedx_learning.models import (
    CompetencyCriteriaGroup,
    CompetencyCriterion,
    CompetencyRuleProfile,
    CompetencyTaxonomy,
    MasteryStatus,
    RuleType,
    StudentCompetencyCriterionStatus,
)
from openedx_tagging.api import (
    delete_object_tags,
    get_tags_locked_for_deletion,
    is_taxonomy_locked_for_deletion,
    lock_object_tags_for_deletion,
)
from openedx_tagging.models import ObjectTag, Tag

from .utils import make_criterion

pytestmark = pytest.mark.django_db


def _score(object_tag: ObjectTag) -> GradedObjectScore:
    """A passing score for the object behind `object_tag`."""
    return GradedObjectScore(object_id=object_tag.object_id, fraction=Decimal("0.9"))


@pytest.mark.usefixtures("criterion")
def test_recording_a_status_locks_the_criterion_object_tag(user, object_tag: ObjectTag) -> None:
    record_graded_object_statuses(user_id=user.id, scores=[_score(object_tag)])

    object_tag.refresh_from_db()
    assert object_tag.deletion_locked is True


def test_second_learner_on_locked_association_succeeds_and_keeps_lock(
    user, criterion: CompetencyCriterion, object_tag: ObjectTag
) -> None:
    record_graded_object_statuses(user_id=user.id, scores=[_score(object_tag)])
    second_user = get_user_model().objects.create(username="learner2")

    changed = record_graded_object_statuses(user_id=second_user.id, scores=[_score(object_tag)])

    assert changed == 1
    assert StudentCompetencyCriterionStatus.objects.filter(criterion=criterion, user_id=second_user.id).exists()
    object_tag.refresh_from_db()
    assert object_tag.deletion_locked is True


def test_tagged_content_with_no_criteria_locks_nothing(user, competency_taxonomy: CompetencyTaxonomy) -> None:
    """Content that carries a tag association but has no competency criterion is graded without locking anything."""
    tag = Tag.objects.create(taxonomy=competency_taxonomy, value="No Criterion")
    object_tag = ObjectTag.objects.create(
        object_id="block-v1:Org1+Python100+Fall2026+problem+none", taxonomy=competency_taxonomy, tag=tag,
    )

    changed = record_graded_object_statuses(user_id=user.id, scores=[_score(object_tag)])

    assert changed == 0
    object_tag.refresh_from_db()
    assert object_tag.deletion_locked is False


@pytest.mark.parametrize(
    "corrupt_kwargs",
    [
        pytest.param({"rule_type_override": "no-such-rule"}, id="unknown_rule_type"),
        pytest.param({"rule_payload_override": {"op": "gte"}}, id="malformed_payload"),
    ],
)
def test_unappliable_rule_leaves_association_unlocked(
    user, competency_taxonomy: CompetencyTaxonomy, corrupt_kwargs: dict
) -> None:
    """A criterion whose rule cannot be applied is skipped, so its association is not locked."""
    bad_tag = Tag.objects.create(taxonomy=competency_taxonomy, value="Bad Rule")
    bad_group = CompetencyCriteriaGroup.objects.create(tag=bad_tag)
    bad_object_tag = ObjectTag.objects.create(
        object_id="block-v1:Org1+Python100+Fall2026+problem+bad", taxonomy=competency_taxonomy, tag=bad_tag,
    )
    bad_criterion = make_criterion(
        group=bad_group, object_tag=bad_object_tag,
        override=(RuleType.GRADE, {"op": "gte", "value": 0.8, "scale": "percent"}),
    )
    CompetencyCriterion.objects.filter(pk=bad_criterion.pk).update(**corrupt_kwargs)

    changed = record_graded_object_statuses(user_id=user.id, scores=[_score(bad_object_tag)])

    assert changed == 0
    bad_object_tag.refresh_from_db()
    assert bad_object_tag.deletion_locked is False


@pytest.mark.parametrize("criteria_count", [1, 3])
def test_lock_is_one_bulk_call_regardless_of_criteria_count(
    user, competency_taxonomy: CompetencyTaxonomy, default_rule_profile: CompetencyRuleProfile, criteria_count: int
) -> None:
    object_id = "block-v1:Org1+Python100+Fall2026+problem+many"
    for index in range(criteria_count):
        tag = Tag.objects.create(taxonomy=competency_taxonomy, value=f"Competency {index}")
        object_tag = ObjectTag.objects.create(object_id=object_id, taxonomy=competency_taxonomy, tag=tag)
        make_criterion(
            group=CompetencyCriteriaGroup.objects.create(tag=tag),
            object_tag=object_tag,
            rule_profile=default_rule_profile,
        )

    with CaptureQueriesContext(connection) as captured:
        changed = record_graded_object_statuses(
            user_id=user.id, scores=[GradedObjectScore(object_id=object_id, fraction=Decimal("0.9"))],
        )

    assert changed == criteria_count
    lock_updates = [
        query["sql"] for query in captured.captured_queries
        if "oel_tagging_objecttag" in query["sql"] and "UPDATE" in query["sql"].upper()
    ]
    assert len(lock_updates) == 1
    assert ObjectTag.objects.filter(object_id=object_id, deletion_locked=True).count() == criteria_count


def test_rolled_back_status_write_leaves_nothing_locked(
    user, criterion: CompetencyCriterion, object_tag: ObjectTag
) -> None:
    """A status write that is rolled back takes its lock with it, since both share the caller's transaction."""

    class _BoomError(Exception):
        """Raised deliberately to force the enclosing transaction to roll back."""

    with pytest.raises(_BoomError), transaction.atomic():
        record_graded_object_statuses(user_id=user.id, scores=[_score(object_tag)])
        assert ObjectTag.objects.values_list("deletion_locked", flat=True).get(pk=object_tag.pk) is True
        raise _BoomError()

    object_tag.refresh_from_db()
    assert object_tag.deletion_locked is False
    assert not StudentCompetencyCriterionStatus.objects.filter(criterion=criterion).exists()


@pytest.mark.usefixtures("criterion")
def test_association_removed_before_the_lock_raises_and_writes_no_status(user, object_tag: ObjectTag) -> None:
    """An association removed after the criteria were read makes the call raise before any status is written."""

    def remove_then_lock(object_tag_ids):
        delete_object_tags(object_tag.object_id)
        return lock_object_tags_for_deletion(object_tag_ids)

    with mock.patch(
        "openedx_learning.applets.cbe.api.lock_object_tags_for_deletion", side_effect=remove_then_lock
    ):
        with pytest.raises(ObjectTag.DoesNotExist):
            record_graded_object_statuses(user_id=user.id, scores=[_score(object_tag)])

    assert not StudentCompetencyCriterionStatus.objects.filter(user_id=user.id).exists()


def test_criterion_whose_status_already_exists_is_still_locked(
    user, criterion: CompetencyCriterion, object_tag: ObjectTag, now
) -> None:
    StudentCompetencyCriterionStatus.objects.create(
        user_id=user.id, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )
    assert object_tag.deletion_locked is False

    changed = record_graded_object_statuses(user_id=user.id, scores=[_score(object_tag)])

    assert changed == 0
    object_tag.refresh_from_db()
    assert object_tag.deletion_locked is True


def test_deleting_locked_association_archives_it_and_protects_tag_and_taxonomy(
    user, criterion: CompetencyCriterion, object_tag: ObjectTag, tag: Tag, competency_taxonomy: CompetencyTaxonomy
) -> None:
    """Deleting a locked association archives it, protects its tag and taxonomy, and keeps the status resolving."""
    record_graded_object_statuses(user_id=user.id, scores=[_score(object_tag)])
    assert get_tags_locked_for_deletion([tag.id]) == {tag.id}
    assert is_taxonomy_locked_for_deletion(competency_taxonomy.id) is True

    delete_object_tags(object_tag.object_id)

    object_tag.refresh_from_db()
    assert object_tag.archived is True
    assert object_tag.deletion_locked is True
    assert get_tags_locked_for_deletion([tag.id]) == {tag.id}
    assert is_taxonomy_locked_for_deletion(competency_taxonomy.id) is True
    resolved = StudentCompetencyCriterionStatus.objects.get(criterion=criterion, user_id=user.id).criterion.object_tag
    assert resolved.pk == object_tag.pk
    assert resolved.archived is True
