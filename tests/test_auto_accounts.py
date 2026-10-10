"""Offline tests for free-form CRM account recognition."""
import pytest
from crm import infer_game, normalize_game, normalize_label, parse_account_template

@pytest.mark.parametrize(("username","game"), [
    ("Dummy3s","Orionstars"),
    ("Michael33s","Orionstars"),
    ("Sarah333fk","Firekirin"),
    ("JANE3V","Vblink"),
    ("Ahsan3u","Ultrapanda"),
    ("Bob33r","Riversweeps"),
    ("Gina3j","Juwa"),
    ("Qasim333gv","GameVault"),
    ("Peter3mw","Milkyway"),
    ("Amy33cm","CashMaster"),
])
def test_suffix_game(username, game):
    assert infer_game(username) == game

def test_message_auto_capture():
    message="👤 Username:Michael333FK\n🔑 Password: 009988\nLog in and let me know if you need help ❤️✨"
    account=parse_account_template(message)
    assert account.game == "Firekirin"
    assert account.username == "Michael333FK"
    assert account.password == "009988"

def test_custom_game_overrides_suffix():
    message="Game: My Special Game\nUsername: Alex3s\nPassword: AbC123"
    account=parse_account_template(message)
    assert account.game == "My Special Game"
    assert account.password == "AbC123"

def test_case_insensitive_labels_and_game():
    assert normalize_label(" VIP ") == normalize_label("vip")
    assert normalize_game("fIrEkIrIn") == "Firekirin"

def test_requires_both_credential_lines():
    assert parse_account_template("👤 Username: Alex3s") is None
