"""Regression checks for wrong-person and partial-cast edition selection."""

import pytest

from rsoul.import_identity import narrator_names, select_edition


AUDIO = {"id": 9, "mediaType": "audiobook"}


def edition(ident, narrator, **extra):
    return {"id": ident, "readingFormatId": 2, "narrator": narrator, **extra}


@pytest.mark.parametrize("heard", ["Porter", "Porter, Ray", "Ray Porter", ["Porter, Ray"]])
def test_shared_surname_does_not_select_a_different_person(heard):
    rows = [edition(1, "Porter, Jane"), edition(2, "Peter Kenny")]
    assert select_edition(AUDIO, rows, "audiobook", heard) is None


@pytest.mark.parametrize("heard, listed", [
    ("Tim Gerard Reynolds", "Reynolds, Tim Gerard"),
    ("Reynolds, Tim Gerard", "Tim Gerard Reynolds"),
    ("Porter, Ray", "Ray Porter"),
    ("Ray Porter", "Porter, Ray"),
    ("Helena Bonham Carter", "Bonham Carter, Helena"),
    ("Müller, Stefan", "Stefan Mu\u0308ller"),
    ("O\u2019Brien, Sean", "Sean O'Brien"),
])
def test_complete_inverted_names_resolve_the_same_person(heard, listed):
    rows = [edition(1, listed), edition(2, "An Unrelated Person")]
    assert select_edition(AUDIO, rows, "audiobook", heard)["id"] == 1


@pytest.mark.parametrize("heard", [
    "Jane Doe; Ray Porter",
    "Jane Doe and Ray Porter",
    "Doe, Jane; Porter, Ray",
    "Doe, Jane, Porter, Ray",
    ["Doe, Jane", "Ray Porter"],
    [["Jane Doe"], ["Porter, Ray"]],
])
def test_complete_cast_lists_and_tag_lists_remain_supported(heard):
    rows = [edition(1, "Murray Porter"), edition(2, ["Jane Doe", "Ray Porter"])]
    assert select_edition(AUDIO, rows, "audiobook", heard)["id"] == 2


def test_one_shared_cast_member_does_not_hide_a_different_narrator():
    rows = [edition(1, ["Jane Doe", "Ray Porter"]), edition(2, "Peter Kenny")]
    assert select_edition(AUDIO, rows, "audiobook", ["Ray Porter", "Someone Else"]) is None


@pytest.mark.parametrize("heard", ["Juan Carlos", "de la Cruz", "Juan Carlos de la Cruz"])
def test_ambiguous_compound_inverted_name_cannot_supply_person_fragments(heard):
    rows = [edition(1, "de la Cruz, Juan Carlos"), edition(2, "Peter Kenny")]
    assert select_edition(AUDIO, rows, "audiobook", heard) is None


def test_unambiguous_full_compound_name_is_preserved():
    rows = [edition(1, "Juan Carlos de la Cruz"), edition(2, "Peter Kenny")]
    assert select_edition(AUDIO, rows, "audiobook", "Juan Carlos de la Cruz")["id"] == 1


@pytest.mark.parametrize("value", ["Jane Doe, Ray Porter", "de la Cruz, Juan Carlos",
                                   "Jane Doe, Ray Porter, John Smith, Peter Kenny"])
def test_ambiguous_comma_only_fields_do_not_create_partial_cast_evidence(value):
    assert narrator_names(value) == set()


def test_the_same_narrator_on_two_editions_stays_ambiguous():
    rows = [edition(1, "Ray Porter"), edition(2, "Porter, Ray")]
    assert select_edition(AUDIO, rows, "audiobook", ["Ray Porter"]) is None


def test_natural_order_is_not_discarded_without_an_inversion_marker():
    rows = [edition(1, "Morgan Taylor"), edition(2, "Peter Kenny")]
    assert select_edition(AUDIO, rows, "audiobook", "Taylor Morgan") is None


@pytest.mark.parametrize("value", [None, True, 1234, {"name": "Ray Porter"}, "Porter", "Ray",
                                   "Ray, Porter, Jane Doe", "Ray Porter,", "Unknown Narrator", "Full Cast"])
def test_incomplete_or_unreadable_metadata_is_not_person_evidence(value):
    assert narrator_names(value) == set()


@pytest.mark.parametrize("key", ["narrator", "narratorNames", "narrators"])
def test_scalar_and_list_metadata_fields_preserve_whole_names(key):
    for value in ("Ray Porter", ["Porter, Ray"]):
        rows = [{"id": 1, "readingFormatId": 2, key: value}, edition(2, "Peter Kenny")]
        assert select_edition(AUDIO, rows, "audiobook", "Ray Porter")["id"] == 1


def test_unique_monitored_edition_retains_priority():
    rows = [edition(1, "Peter Kenny", monitored=True), edition(2, "Ray Porter")]
    assert select_edition(AUDIO, rows, "audiobook", "Ray Porter")["id"] == 1


def test_narrators_cannot_select_an_edition_of_another_medium():
    rows = [{"id": 1, "readingFormatId": 3, "narrator": "Ray Porter"},
            edition(2, "Peter Kenny"), edition(3, "Murray Porter")]
    assert select_edition(AUDIO, rows, "audiobook", "Ray Porter") is None


@pytest.mark.parametrize("metadata", ["Ray Porter", ["Porter, Ray"], [["Ray Porter"]]])
def test_import_preparation_preserves_complete_scalar_or_list_narrator_tags(metadata):
    from rsoul.import_transaction import prepare_command

    class Client:
        ver_uri = "/api/v1"

        def get_edition(self, book_id):
            return [edition(1, "Ray Porter"), edition(2, "Murray Porter")]

        def _get(self, endpoint, version, params):
            assert endpoint == "manualimport"
            return [{"path": "/remote/Book/01.mp3", "quality": {"quality": {"id": 1}}}]

    pending = {"method": "targeted", "book_id": 9, "media_type": "audiobook",
               "readarr_path": "/remote/Book", "audio_validation": [{"metadata": {"narrator": metadata}}],
               "manifest": [{"readarr_path": "/remote/Book/01.mp3"}]}
    payload, edition_id, error = prepare_command(Client(), pending, dict(AUDIO, authorId=3))
    assert error == "" and edition_id == 1
    assert payload["files"][0]["editionId"] == 1
