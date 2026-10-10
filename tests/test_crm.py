"""Tests for user-configurable labels, game names and usernames."""
import pytest
from crm import (
    parse_game, parse_source, normalize_game, normalize_label, label_display,
    normalize_workflow, safe_username, topic_title, profile_text,
    parse_account_template, infer_game,
)

def test_builtin_game_names_ignore_case():
    assert parse_game(['JUWA']) == 'Juwa'
    assert parse_game(['vBLinK']) == 'Vblink'
    assert parse_game(['cashmaster']) == 'CashMaster'
    assert parse_game(['missing']) is None

def test_custom_game_names_preserve_user_text():
    assert normalize_game('FIRE KIRIN') == 'Firekirin'
    assert normalize_game('My Special Game') == 'My Special Game'

@pytest.mark.parametrize('text', ['VIP','ViP','vip'])
def test_case_insensitive_labels(text):
    assert normalize_label(text)=='vip'

def test_arbitrary_labels():
    assert normalize_label('High Roller')=='high roller'
    assert normalize_label('Needs Follow-Up')=='needs follow-up'
    assert label_display('high roller','High Roller')=='High Roller'

def test_usernames_preserve_case_and_custom_characters():
    assert safe_username('@Player123') == 'Player123'
    assert safe_username('Player Name') == 'Player Name'
    assert safe_username('some/password') == 'some/password'
    with pytest.raises(ValueError):safe_username('')
    with pytest.raises(ValueError):safe_username('x'*90)

def test_workflow_custom():
    assert normalize_workflow('In Progress') == 'in_progress'
    assert normalize_workflow('call later') == 'call_later'

@pytest.mark.parametrize(('username','game'),[
    ('Bob3s','Orionstars'), ('Sarah33s','Orionstars'),
    ('Ali333fk','Firekirin'), ('Alice3u','Ultrapanda'),
    ('Chris3v','Vblink'), ('Nina3r','Riversweeps'),
    ('James3j','Juwa'), ('John33gv','GameVault'),
    ('Mary333mw','Milkyway'), ('Amy33cm','CashMaster'),
])
def test_credential_suffix_detection(username,game):
    assert infer_game(username)==game

def test_credential_message_and_explicit_override():
    a=parse_account_template('👤 Username: Dummy333fk\n🔑 Password: 009988')
    assert (a.game,a.username,a.password)==('Firekirin','Dummy333fk','009988')
    b=parse_account_template('Game: Custom Label Game\nUsername: RealPlayer\nPassword: 1234')
    assert b.game=='Custom Label Game' and b.username=='RealPlayer'

def test_pinned_profile_hides_passwords():
    row={'user_id':12,'name':'Sarah','username':'sarah','primary_label':'vip',
         'game':'CashMaster','source':'Telegram Ads','first_touch_campaign':'tgads_v1',
         'last_touch_campaign':'tgads_v2'}
    message=profile_text(row,[{'game':'CashMaster','account_username':'Sarah33cm'}],
                         [{'label':'vip'}],[],{'count':0,'amount':0})
    assert 'WINNING WAVE' in message
    assert 'Sarah33cm' in message
    assert 'tgads_v1' in message
    assert '009988' not in message

def test_topic_title():
    title=topic_title({'user_id':12,'name':'Sarah','primary_label':'vip','game':'CashMaster'})
    assert title.startswith('👑 VIP | Sarah')
