import pytest

from frontend_user.core.feedback import build_feedback


def test_general_feedback_body_is_normalized() -> None:
    body = build_feedback(
        feedback_type="GENERAL", rating=5, comment="  재미   있어요 ", tags=["UX"]
    )
    assert body == {
        "feedback_type": "GENERAL",
        "rating": 5,
        "comment": "재미 있어요",
        "tags": ["UX"],
    }


@pytest.mark.parametrize("rating", [0, 6])
def test_rating_must_be_one_to_five(rating: int) -> None:
    with pytest.raises(ValueError):
        build_feedback(feedback_type="GENERAL", rating=rating, comment="", tags=[])


def test_game_feedback_requires_uuid() -> None:
    with pytest.raises(ValueError):
        build_feedback(feedback_type="GAME", rating=4, comment="좋아요", tags=[], game_id="invalid")


def test_comment_and_tag_boundaries_are_enforced() -> None:
    with pytest.raises(ValueError):
        build_feedback(feedback_type="GENERAL", rating=4, comment="가" * 1001, tags=[])
    with pytest.raises(ValueError):
        build_feedback(feedback_type="GENERAL", rating=4, comment="", tags=["UX", "UX"])
