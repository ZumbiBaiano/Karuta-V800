from karuta.discord_page import is_drop_text


def test_is_drop_text_3_cards():
    assert is_drop_text("@user is dropping 3 cards!")


def test_is_drop_text_singular_and_other_count():
    assert is_drop_text("Foo is dropping 1 card!")
    assert is_drop_text("Bar is dropping 4 cards!")


def test_is_drop_text_negative():
    assert not is_drop_text("Olha as cartas que dropei")
    assert not is_drop_text("")
    assert not is_drop_text(None)  # type: ignore[arg-type]
