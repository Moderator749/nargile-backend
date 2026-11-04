"""The menu's depth rules and the storefront's visible set, on plain data."""

from app.services.categories import ancestors, height, placement_error, visible_ids

# 1 Nargile Takımları > 2 Oduman Nargile > 3 Oduman Lüleler; 4 Aksesuar alone; 5 > 6 a branch of two.
TREE = {1: None, 2: 1, 3: 2, 4: None, 5: None, 6: 5}


def test_ancestors_nearest_first_and_safe_on_a_loop():
    assert ancestors(3, TREE) == [2, 1]
    assert ancestors(1, TREE) == []
    assert ancestors(1, {1: 2, 2: 1}) == [2]


def test_height_counts_the_levels_below():
    assert height(1, TREE) == 3
    assert height(2, TREE) == 2
    assert height(3, TREE) == 1
    assert height(7, {7: 8, 8: 7}) >= 1  # a loop ends


def test_placement():
    assert placement_error(None, 2, TREE) is None  # a third level
    assert "en fazla 3 seviye" in placement_error(None, 3, TREE)  # a fourth
    assert placement_error(4, 2, TREE) is None  # a leaf into the third level
    assert placement_error(5, 1, TREE) is None  # a two-level branch fits under a group
    assert "3 seviyeyi aşar" in placement_error(5, 2, TREE)  # but not one level lower
    assert "3 seviyeyi aşar" in placement_error(1, 4, TREE)  # a three-level branch under anything
    assert "kendi alt kategorisinin" in placement_error(1, 3, TREE)
    assert "kendi alt kategorisinin" in placement_error(2, 2, TREE)


def test_visible_needs_every_category_above_on():
    rows = [(1, None, True), (2, 1, True), (3, 2, True), (4, None, False), (5, 4, True), (6, 5, True)]
    assert visible_ids(rows) == {1, 2, 3}
    assert visible_ids([(1, None, False), (2, 1, True), (3, 2, True)]) == set()
    assert visible_ids([(1, None, True), (2, 1, False), (3, 2, True)]) == {1}
    # A parent missing, or a loop: shown nowhere.
    assert visible_ids([(2, 9, True)]) == set()
    assert visible_ids([(1, 2, True), (2, 1, True), (3, None, True)]) == {3}
