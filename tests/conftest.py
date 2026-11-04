"""テスト共通の設定。

ごみ箱へ移す直前の確認ダイアログ（FinalPage.confirm）は、画面のないテストでは応答できないため、
既定で「承認した」として扱う。承認しなかった場合の動きは、個別のテストで置き換えて確認する。
本物の Windows のごみ箱は、各テストが偽の移動処理（backend）を渡すことで使わない。
"""

import pytest


@pytest.fixture(autouse=True)
def _approve_final_confirmation(monkeypatch):
    try:
        from storage_organizer import organizer
    except ImportError:  # PySide6 がない環境（GUI のテストは各ファイルで skip される）
        yield
        return
    monkeypatch.setattr(organizer.FinalPage, "confirm", lambda self, count, total: True)
    yield
