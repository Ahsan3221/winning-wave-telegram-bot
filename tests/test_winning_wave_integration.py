"""Winning Wave add-on smoke tests. Production Telegram and Railway are not contacted."""
import importlib
from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]

def test_legacy_campaigns_and_crm_hooks_are_both_present():
    source = (ROOT / 'bot.py').read_text()
    ast.parse(source)
    for name in ('winning_wave_campaigns','winning_wave_events','crm_addon.group_message',
                 'crm_addon.enqueue_incoming','crm_addon.post_init','adstats_command',
                 'campaigns_command','menu_callback'):
        assert name in source

def test_brand_and_db_namespace():
    source=(ROOT/'database.py').read_text()
    assert 'winning_wave_users' in source
    assert 'winning_wave_accounts' in source
    assert 'ace_arcade' not in source
    assert 'DROP TABLE' not in source
    assert 'DELETE FROM winning_wave_events' in source
    assert 'WINNING WAVE' in (ROOT/'crm.py').read_text()

def test_always_include_cashmaster_and_preserve_custom_games(monkeypatch):
    import settings
    monkeypatch.setenv('ALLOWED_GAMES','Juwa,SpecialGame')
    try:
        settings=importlib.reload(settings)
        assert 'CashMaster' in settings.GAMES
        assert 'SpecialGame' in settings.GAMES
    finally:
        monkeypatch.delenv('ALLOWED_GAMES')
        importlib.reload(settings)

def test_custom_game_username_password_template():
    from crm import parse_account_template
    inferred=parse_account_template('👤 Username:Maria333CM\n🔑 Password: 009988')
    assert (inferred.game,inferred.username,inferred.password) == ('CashMaster','Maria333CM','009988')
    explicit=parse_account_template('Game: Custom New Game\nUsername: Alice\nPassword: 1234')
    assert explicit.game=='Custom New Game'
