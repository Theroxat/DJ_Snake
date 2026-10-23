"""Translations: catalog parity, language choice and French replies."""

import json
import re
import string

from djsnake import commands, config, i18n, state
from djsnake.i18n import t
from djsnake.sessions import render_panel
from fakes import FakeChannel, make_ctx, make_track

GUILD_ID = 1


def placeholders(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


# --- Catalogs -----------------------------------------------------------------


def test_every_language_has_the_same_keys_and_placeholders():
    english = i18n.CATALOGS["en"]
    assert set(i18n.LANGUAGES) >= {"en", "fr"}
    for lang, catalog in i18n.CATALOGS.items():
        assert set(catalog) == set(english), lang
        for key, text in catalog.items():
            assert placeholders(text) == placeholders(english[key]), (lang, key)


def test_plural_keys_come_in_pairs():
    for key in i18n.CATALOGS["en"]:
        if key.endswith((".one", ".other")):
            base = key.rsplit(".", 1)[0]
            assert {f"{base}.one", f"{base}.other"} <= set(i18n.CATALOGS["en"])


def test_french_typography():
    for key, text in i18n.CATALOGS["fr"].items():
        assert not re.search(r" [:;!?]", text), key  # No-break space instead
        assert "'" not in text, key  # Typographic apostrophe
        assert "..." not in text, key  # Ellipsis character


def test_catalogs_are_valid_utf8_json():
    for lang in i18n.LANGUAGES:
        with open(f"{i18n.LOCALES_DIR}/{lang}.json", encoding="utf-8") as f:
            assert isinstance(json.load(f), dict)


# --- Lookup -------------------------------------------------------------------


def test_lookup_and_fallbacks(monkeypatch):
    assert t("panel.expired") == "⌛ Session expired"  # BOT_LANGUAGE=en
    assert t("panel.expired", "fr") == "⌛ Session expirée"
    assert t("panel.expired", "fr-CA") == "⌛ Session expirée"
    assert t("panel.expired", "de") == "⌛ Session expired"
    assert t("not a key at all {x}") == "not a key at all {x}"
    monkeypatch.delitem(i18n.CATALOGS["fr"], "panel.expired")
    assert t("panel.expired", "fr") == "⌛ Session expired"


def test_prefix_and_plurals():
    assert t("cmd.play_usage", "en").startswith("❌ Usage: `!play")
    assert t("panel.up_next_more", "fr", count=1) == " et 1 autre"
    assert t("panel.up_next_more", "fr", count=0) == " et 0 autre"
    assert t("panel.up_next_more", "fr", count=2) == " et 2 autres"
    assert t("load.done", "en", loaded=1, count=1) == "✅ 1/1 tracks loaded"
    assert t("load.done", "fr", loaded=1, count=1) == "✅ 1/1 titre chargé"


def test_normalize():
    assert i18n.normalize("FR") == "fr"
    assert i18n.normalize("fr_FR") == "fr"
    assert i18n.normalize("en-US") == "en"
    assert i18n.normalize("xx") is None
    assert i18n.normalize(None) is None


# --- Guild language -------------------------------------------------------------


def test_guild_language_is_saved_and_reloaded(data_dir):
    assert i18n.lang_for(GUILD_ID) == "en"
    i18n.set_language(GUILD_ID, "fr")
    assert i18n.lang_for(GUILD_ID) == "fr"
    assert i18n.lang_for(2) == "en"
    with open(data_dir / "languages.json") as f:
        assert json.load(f) == {"1": "fr"}
    state.guild_languages.clear()
    i18n.load_languages()
    assert i18n.lang_for(GUILD_ID) == "fr"
    i18n.set_language(GUILD_ID, None)
    assert i18n.lang_for(GUILD_ID) == "en"


def test_bad_language_file_entries_are_dropped(data_dir):
    data_dir.mkdir(exist_ok=True)
    (data_dir / "languages.json").write_text('{"1": "fr", "2": "xx", "a": "fr"}')
    i18n.load_languages()
    assert state.guild_languages == {1: "fr"}


async def test_language_command(ctx, channel):
    await commands.language.callback(ctx, "fr")
    assert state.guild_languages[GUILD_ID] == "fr"
    assert channel.reactions == ["✅"]
    await commands.language.callback(ctx, None)
    assert "Langue : **Français**" in channel.log[-1]
    await commands.language.callback(ctx, "klingon")
    assert channel.log[-1].startswith("❌ Langue inconnue")
    await commands.language.callback(ctx, "reset")
    assert GUILD_ID not in state.guild_languages
    await commands.language.callback(ctx, None)
    assert "Language: **English**" in channel.log[-1]


def test_language_command_needs_manage_guild():
    assert len(commands.language.checks) == 1


# --- French replies ---------------------------------------------------------------


async def test_french_command_replies(ctx, channel):
    state.guild_languages[GUILD_ID] = "fr"
    await commands.list_queue.callback(ctx)
    assert channel.log[-1] == "❌ La file d’attente est vide."
    await commands.next_track.callback(ctx)
    assert channel.log[-1] == "❌ Aucune musique en cours de lecture."
    other = FakeChannel()
    await commands.list_queue.callback(make_ctx(2, other))
    assert other.log[-1] == "❌ The queue is empty."


async def test_french_panel(player, ctx, media_dir):
    player.add_to_queue(make_track(media_dir, "T0"))
    player.add_to_queue(make_track(media_dir, "T1"))
    assert "Up next: T0 and 1 more" in render_panel(player)
    state.guild_languages[GUILD_ID] = "fr"
    text = render_panel(player)
    assert "📺 Regarder le clip en direct :" in text
    assert "⏭️ À suivre : T0 et 1 autre" in text


async def test_french_help(ctx, channel):
    state.guild_languages[GUILD_ID] = "fr"
    helper = commands.HelpCommand()
    ctx.bot = state.bot
    helper.context = ctx
    await helper.send_bot_help({})
    text = channel.log[-1]
    assert text.startswith("🎶 **Commandes de DJ Snake**")
    assert "`!language [code|reset]` :" in text
    assert text.index("`!play") < text.index("`!list")


# --- Web ------------------------------------------------------------------------


async def test_state_lang_and_localised_api_errors(client, player):
    r = await client.get(f"/api/{player.sid}/state")
    assert (await r.json())["lang"] == "en"
    state.guild_languages[GUILD_ID] = "fr"
    r = await client.get(f"/api/{player.sid}/state")
    assert (await r.json())["lang"] == "fr"
    r = await client.post(f"/api/{player.sid}/control", json={"action": "pause"})
    body = await r.json()
    assert r.status == 409
    assert body == {"ok": False, "error": "Aucune musique en cours de lecture."}
    r = await client.post(f"/api/{player.sid}/control", json={"action": "bogus"})
    assert (await r.json())["error"] == "Action inconnue."
    r = await client.post("/api/nosuchsid/control", json={"action": "next"})
    assert (await r.json())["error"] == "Unknown or expired session."


def _page_dictionary(page, lang):
    block = re.search(rf"\n    {lang}: {{\n(.*?)\n    }}", page, re.S).group(1)
    return dict(re.findall(r'^\s+(\w+): "(.*)",?$', block, re.M))


def test_page_dictionaries_match():
    with open(config.WEB_INDEX, encoding="utf-8") as f:
        page = f.read()
    english, french = _page_dictionary(page, "en"), _page_dictionary(page, "fr")
    assert len(english) > 100
    assert set(english) == set(french)
    for key, text in english.items():
        assert set(re.findall(r"{(\w+)}", text)) == set(
            re.findall(r"{(\w+)}", french[key])
        ), key
