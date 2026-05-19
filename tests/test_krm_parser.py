from karuta.discord_page import parse_krm_reply


def test_ready_lines():
    text = "Drop is ready\nGrab is ready\nEffect is ready"
    out = parse_krm_reply(text)
    assert out == {"drop": 0, "grab": 0, "effect": 0}


def test_minutes_and_seconds():
    text = "Drop: 25m 12s\nGrab: 5m 30s"
    out = parse_krm_reply(text)
    assert out["drop"] == 25 * 60 + 12
    assert out["grab"] == 5 * 60 + 30


def test_only_minutes():
    text = "Grab: 12m\nEffect: 45s"
    out = parse_krm_reply(text)
    assert out["grab"] == 12 * 60
    assert out["effect"] == 45


def test_will_be_ready_in():
    text = "Grab will be ready in 2m 5s"
    out = parse_krm_reply(text)
    assert out["grab"] == 2 * 60 + 5


def test_emoji_prefix():
    text = "✋ Grab is ready!\n🔻 Drop: 8m"
    out = parse_krm_reply(text)
    assert out["grab"] == 0
    assert out["drop"] == 8 * 60


def test_empty():
    assert parse_krm_reply("") == {}
    assert parse_krm_reply("nada relevante aqui") == {}
