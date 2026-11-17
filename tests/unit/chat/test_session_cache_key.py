import pytest

from app.core.utils.llm_request_params import build_session_cache_key


def test_session_cache_key_is_stable_hex_and_hides_identity():
    uid = "uid-with-long-private-identity-7f3a9c2e"
    session_id = "session-with-long-private-identity-91b4d8a6"

    key = build_session_cache_key(uid=uid, session_id=session_id)

    assert key == build_session_cache_key(uid=uid, session_id=session_id)
    assert len(key) == 64
    assert all(character in "0123456789abcdef" for character in key)
    assert uid not in key
    assert session_id not in key


def test_session_cache_key_preserves_main_key_without_subagent_id():
    expected = "40123983ef2e0e616ed54d345648e59468dc27ed3cd815ceafb0f75e3798637d"

    assert build_session_cache_key(uid="user-main", session_id="session-main") == expected
    assert build_session_cache_key(uid="user-main", session_id="session-main", subagent_id=None) == expected


def test_session_cache_key_separates_main_and_subagent_keys():
    uid = "user-main"
    session_id = "session-main"
    subagent_id = "subagent-a"
    other_subagent_id = "subagent-b"

    main_key = build_session_cache_key(uid=uid, session_id=session_id)
    subagent_key = build_session_cache_key(uid=uid, session_id=session_id, subagent_id=subagent_id)
    repeated_key = build_session_cache_key(uid=uid, session_id=session_id, subagent_id=subagent_id)
    other_subagent_key = build_session_cache_key(
        uid=uid,
        session_id=session_id,
        subagent_id=other_subagent_id,
    )

    assert subagent_key == repeated_key
    assert len(subagent_key) == 64
    assert all(character in "0123456789abcdef" for character in subagent_key)
    assert all(identity not in subagent_key for identity in (uid, session_id, subagent_id))
    assert len({main_key, subagent_key, other_subagent_key}) == 3


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            ("user-main", "session-main", "subagent-a"),
            ("user-other", "session-main", "subagent-a"),
        ),
        (
            ("user-main", "session-main", "subagent-a"),
            ("user-main", "session-other", "subagent-a"),
        ),
    ],
)
def test_session_cache_key_changes_when_identity_changes_with_subagent(first, second):
    first_key = build_session_cache_key(uid=first[0], session_id=first[1], subagent_id=first[2])
    second_key = build_session_cache_key(uid=second[0], session_id=second[1], subagent_id=second[2])

    assert first_key != second_key


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            ("user-alpha-with-long-identity", "session-fixed-with-long-identity"),
            ("user-beta-with-long-identity", "session-fixed-with-long-identity"),
        ),
        (
            ("user-fixed-with-long-identity", "session-alpha-with-long-identity"),
            ("user-fixed-with-long-identity", "session-beta-with-long-identity"),
        ),
    ],
)
def test_session_cache_key_changes_when_one_identity_part_changes(first, second):
    first_key = build_session_cache_key(uid=first[0], session_id=first[1])
    second_key = build_session_cache_key(uid=second[0], session_id=second[1])

    assert first_key != second_key


def test_session_cache_key_keeps_identity_parts_unambiguous():
    assert build_session_cache_key(uid="alpha:beta", session_id="gamma") != build_session_cache_key(uid="alpha", session_id="beta:gamma")


def test_session_cache_key_supports_unicode_quotes_and_backslashes():
    uid = '用户"甲"\\标识'
    session_id = '会话"一"\\标识'
    other_uid = '用户"乙"\\标识'
    other_session_id = '会话"二"\\标识'

    key = build_session_cache_key(uid=uid, session_id=session_id)

    assert key == build_session_cache_key(uid=uid, session_id=session_id)
    assert key != build_session_cache_key(uid=other_uid, session_id=other_session_id)


def test_session_cache_key_keeps_three_special_character_fields_stable_and_unambiguous():
    first = {
        "uid": '用户:甲"\\标识',
        "session_id": '会话:一"\\标识',
        "subagent_id": '子代理:乙"\\标识',
    }
    ambiguous = {
        "uid": "用户",
        "session_id": '甲"\\标识:会话:一"\\标识',
        "subagent_id": '子代理:乙"\\标识',
    }

    first_key = build_session_cache_key(**first)

    assert ":".join(first.values()) == ":".join(ambiguous.values())
    assert first_key == build_session_cache_key(**first)
    assert first_key != build_session_cache_key(**ambiguous)
