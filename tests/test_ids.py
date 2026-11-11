import time

from superagent.core.ids import new_id, slug, unique_name


def test_new_id_has_prefix_and_fixed_length():
    value = new_id("tk")
    assert value.startswith("tk_")
    assert len(value) == len("tk_") + 22


def test_new_ids_sort_by_creation_time():
    first = new_id("tk")
    time.sleep(0.002)
    second = new_id("tk")
    assert first < second


def test_new_ids_are_unique():
    assert len({new_id("x") for _ in range(1000)}) == 1000


def test_slug_normalizes_text():
    assert slug("Fix: the Save button on Settings!") == "fix-the-save-button-on-settings"


def test_slug_strips_accents():
    assert slug("Değişiklik özeti çalışmıyor") == "degisiklik-ozeti-calismiyor"


def test_slug_respects_max_length_on_word_boundaries():
    assert slug("alpha beta gamma delta", max_length=12) == "alpha-beta"


def test_slug_truncates_a_single_long_word():
    assert slug("a" * 50, max_length=10) == "a" * 10


def test_slug_falls_back_for_empty_text():
    assert slug("!!!") == "ticket"


def test_unique_name_appends_counter():
    assert unique_name("fix", set()) == "fix"
    assert unique_name("fix", {"fix"}) == "fix-2"
    assert unique_name("fix", {"fix", "fix-2"}) == "fix-3"
