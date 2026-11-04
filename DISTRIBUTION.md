# RecSort 配布手順（リリース候補 0.9.0rc2）

## 対応環境
- Windows 10 / 11（64ビット）。主な対象は Windows 11
- ソースから起動する場合: Python 3.12 以上
- FFmpeg（ffmpeg と ffprobe）: 任意。ない場合も重複の検出と容量の集計はできるが、録画の長さ・プレビュー画像・失敗録画の判定は使えない

## 配布版（exe）の作り方（Windows で実行）
1. Python 3.12 以上をインストールする（「Add python.exe to PATH」にチェック）
2. ソース一式のフォルダで `packaging\build_windows.bat` を実行する
3. `dist\RecSort\RecSort.exe` ができる。`dist\RecSort` フォルダごと ZIP にして配布する

- 配布物に含まれないもの: 開発用の DB、サムネイルのキャッシュ、ログ、テスト、個人のパス（データはすべて実行時に `%LOCALAPPDATA%\RecSort` へ作られる）
- この手順は Linux の開発環境では実行していない（PyInstaller は動かす OS 向けの exe しか作れない）。Windows 実機での作成と確認が必要

## Python が入っていない PC での確認項目
1. ZIP を展開し、`RecSort.exe` をダブルクリックして起動する（SmartScreen の警告が出たら「詳細情報」→「実行」）
2. ホームが表示され、「調べる場所を選ぶ」から調査できる
3. FFmpeg がない状態で、ホームに「FFmpeg が見つかりません」の案内と「設定を開く」が出る
4. 設定で ffmpeg の `bin` フォルダを指定すると案内が消える
5. 録画を選ぶとプレビューが再生される（Qt Multimedia が同梱されていること）
6. 整理候補 → 最終確認 → ごみ箱への移動が、テスト用のファイルでできる
7. アプリを閉じて起動し直しても、保護と判断が残っている
8. `_internal\licenses` にライセンス全文（Qt・FFmpeg・numpy・Pillow・Python・PyInstaller）が入っている
10. `%LOCALAPPDATA%\RecSort` に `settings.json`・`inventory`・`thumbs`・`logs` などが作られ（データベースファイルは作られない）、exe のフォルダには何も書き込まれない

## データの保存場所（データベースは使わない）
すべて `%LOCALAPPDATA%\RecSort` に作られ、exe のフォルダには書き込みません。内容と形式は README の「データの保存」を参照。

| 種類 | 場所 |
|---|---|
| 設定・保護・判断 | settings.json、user_state.json |
| 調査結果・容量の集計 | scan_state.json、inventory\*.json.gz、analysis.jsonl |
| ごみ箱への移動の記録 | trash_log.jsonl |
| サムネイル・比較用の場面（上限 300MB） | thumbs\ |
| エラーログ（1MB で作り直し、ホームフォルダのパスは伏せる、録画の内容は記録しない） | logs\recsort-error.log |

以前の版のデータ（`%LOCALAPPDATA%\RecSortAI`）は削除せず、設定画面から取り込めます。

## 問題の報告方法
- 画面のスクリーンショットと、`%LOCALAPPDATA%\RecSort\logs\recsort-error.log`（ある場合）をチームに送ってください
- 録画ファイルそのものは送らないでください

## 既知の制限
README の「既知の制限」を参照してください。
