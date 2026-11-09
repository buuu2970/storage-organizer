# 統合の記録（integration/recsort-final）

## 採用したブランチとコミット

| ブランチ | コミット | 扱い |
|---|---|---|
| shuhei | 7ac00eb | **土台**。自動スキャン・整理候補・比較・最終確認・JSON 保存と復旧・二重起動防止・デスクトップ UI |
| kdt26aie40012-byte（ブランチ3） | b5eb75f | WinGet での FFmpeg 検出、音声の判定（無音）の考え方だけを取り込み。Web UI・CLI・削除予定フォルダ方式は取り込まない |
| kdt26aie40003-oss（ブランチ1） | ce26e4e | 機械学習（初期12本・ジャンル分類・厳選 AI・プロファイル）と SQLite は取り込まない（お気に入りの操作は試作版 af51973 由来で、統合版の画面に作り直した）。元のブランチにそのまま残る |

0d85a3d ではなく 7ac00eb を土台にした理由: 0d85a3d は保護の記録が壊れると保護なしで起動してしまう。
7ac00eb の安全面の修正（保護データの復旧待ち・二重起動防止・残す側の再確認・世代番号）は複数ファイルにまたがり、部分的に移せないため。

## 取り込まなかったもの

- SQLite（legacy_db.py）と旧データの移行（migrate.py）: データベース全面禁止のため。旧データ（RecSortAI・RecSort）は読み書きしない
- 機械学習（ai.py・pages.py の AI 画面・初期12本）: 今回は対象外。将来追加するときは、学習データだけをプロファイル別に持ち、
  判断・保護・お気に入り・調査結果は共通のままにする。初期12本の動画は、公開・配布前に出典と利用許諾の確認が必要
- 画像の整理（類似画像・画像の整理画面）: 録画の整理を優先。コード（candidates.analyze_image など）は残し、
  `autoscan.KIND_OF_CATEGORY` に画像を足せば登録できる
- ブランチ3のぼやけ判定・音割れ判定: 内容（暗い場面・アニメ・音楽）で誤検出しやすいため
- 動画の圧縮（ブランチ1）: 一時的に容量が増え、「容量がいっぱい」の状況と合わないため

## 主な変更

- ごみ箱: `recycle_bin.py`（IFileOperation + 進捗通知でフェイルクローズ）。`trash.py` の安全確認はそのまま使う
- 保存: 書き込み後の確認・schema_version・まとめ保存・保存先 `RecSortV1`
- 候補: お気に入り除外・目標なしでも客観的な問題を要確認として表示・無音判定・文言（破損と断定しない）
- 画面: 5画面（ホーム・ファイル一覧・整理のおすすめ・削除候補・設定）、用語の統一と説明、最終確認ダイアログ
- 削除したファイル: legacy_db.py, migrate.py, ai.py, pages.py, file_table_model.py, tests/test_recsort.py, tests/test_gui.py
- 追加したファイル: recycle_bin.py, tests/test_recycle_bin.py, tests/test_integration.py, tests/conftest.py,
  tools/recycle_bin_live_test.py

## テスト結果（2026-11-04、Windows 11、Python 3.14.3、PySide6 6.12.0、FFmpeg なし）

- `pytest tests`: 183 passed, 16 skipped
- `python -m unittest discover -s tests`（配布用ビルドと同じ方法）: 199 件 OK（16 skipped）
- スキップの内訳: FFmpeg が必要なもの 13、Windows 以外でだけ行う試験 1、シンボリックリンクの作成権限 1、本物のごみ箱を使う試験 1（下記のツールで実施）
- ごみ箱の実機試験（`tools/recycle_bin_live_test.py`）: ダミーファイルをメインスレッドと別スレッドで
  ごみ箱へ移動 → ごみ箱内で名前・元の場所を確認 → 復元 → SHA-256 一致。ディレクトリ・存在しないパス・相対パスは拒否。
  完全削除の指示（TSF_DELETE_RECYCLE_IF_POSSIBLE なし）を模擬すると中止され、元のファイルが残ることを確認
- 画面の起動確認: ダミーの録画フォルダで5画面を表示し、削除候補 → 最終確認 → 確認で「いいえ」なら移動しないことを確認

## 統合後に入った変更（11/04〜11/09）

| コミット | 内容 |
|---|---|
| d102cd5 | ごみ箱への移動と調査を同時に実行できないよう、内部で原子的に防ぐ（tests/test_concurrency.py を追加）。Pillow 14 で廃止予定の `getdata()` の置き換え、Windows 以外・PySide6 なしの環境でのテスト修正 |
| e3fd0a1 | 色の整理（theme.py・organizer.py・player.py）。あわせて AI の厳選・ジャンル予測（ai_curation.py・profiles.py・samples.py）と動画の圧縮（media.py）も入ったが、どこからも呼ばれていなかった |
| 1756996 | PR #3（beginner-ux-ai → main）。上の2件と統合後の内容が main に入った |
| f3191ea | 統合の方針（機械学習は対象外）に合わせて、e3fd0a1 の AI 関連ファイルと、AI のためだけの「動画の特徴」の保存を削除 |
| b46790a | kdt26aie40003-oss ブランチの Web 版（`web/`、Codex で作成）を取り込み。ブランチの機械学習・プロファイル（3979bd4・892e56d）は取り込まない |
| （このコミット） | e3fd0a1 で入った、使われていない動画の圧縮処理を media.py から削除（「取り込まなかったもの」の方針どおり）。README と発表資料を更新 |

旧 Web 簡易版（web/）との違いは README の「旧 Web 簡易版」を参照。

## ブラウザ版への切り替え（11/09）

利用者の判断で、「自分のパソコンでブラウザの画面として使う」形を標準にした。ごみ箱への移動は残す。
デスクトップ版（PySide6）は画面と操作の手本として残し、`python main.py --desktop` で起動できる。

- `storage_organizer/webapp.py`: 標準ライブラリの http.server によるサーバー。127.0.0.1 だけで待ち受け、
  起動ごとの合言葉（トークン）と Host ヘッダーを確認する。調査・候補・保存・ごみ箱への移動は既存の処理
  （autoscan・candidates・store・trash）をそのまま呼ぶ。二重起動のロックもデスクトップ版と共通
- `storage_organizer/web_ui/`: 画面（index.html・app.js・style.css）。5画面・用語・進行表示・比較・最終確認・結果を
  デスクトップ版に合わせ、配色も theme.py と同じにした。外部のライブラリやサイトは使わない
- 録画の再生はブラウザの `<video>`（登録した録画だけを、Range 対応で返す）。ブラウザが再生できない形式は保存場所を開いて確認
- `main.py`: 引数なしでブラウザ版、`--desktop` でデスクトップ版
- テスト: tests/test_webapp.py（11 件。合言葉・Host の確認、静的ファイルの外を読ませない、登録外の録画を返さない、
  保護中は削除候補にしない、最終確認のない移動を断る、重複の最後の1本を残す、確認後に調査結果が変わったら止める ほか）。
  StartupTest はデスクトップ版とブラウザ版の両方の起動を確認
- 画面の確認: ダミーの録画でサーバーを動かし、ヘッドレスの Chrome で5画面の表示を確認。
  **実際のブラウザでの操作（ごみ箱への移動まで）と、本物の録画の再生は未確認**

### テスト結果（2026-11-09、Windows 11、Python 3.14.3、PySide6 6.12.0、FFmpeg なし）

- `pytest tests`: 197 passed, 16 skipped（スキップの内訳は上と同じ）
- `python -m unittest discover -s tests`: 213 件 OK（16 skipped）
- `test_review_fixes.py::InstanceLockTest::test_second_app_instance_exits_without_touching_data` が、
  実行するたびに成功したり失敗したりしていた（6 回中 2〜3 回失敗。統合直後の 2892fe4 でも同じ）。
  ロックを持つプロセスを終了させた直後に一時フォルダの instance.lock を消すと、Windows がまだファイルを解放しておらず
  WinError 32（ほかのプロセスが使用中）になっていた。アプリの不具合ではない。
  テスト側で、一時フォルダを消すときに少し待って消し直すよう修正（15 回続けて成功）
- 旧 Web 簡易版（`web/`）には自動テストがない

## 未検証・既知の制限

- FFmpeg を使う判定（長さ・黒画面・無音・類似録画・サムネイル）は、この PC に FFmpeg がないため自動テストで未実行
- Windows が実際に完全削除へ切り替える状況（ごみ箱の容量超過・NukeOnDelete・ごみ箱のないドライブ）は、安全に再現できないため実機では未確認
  （模擬とモックで確認済み）。固定ドライブ以外の拒否もモックでのみ確認
- ごみ箱から復元する試験では、Windows が復元後に `$I` メタデータ（各 460 バイト程度、ごみ箱には表示されない）を残すことがある
- 保護・判断はパスで記録しているため、ファイルを移動・名前変更すると引き継がれない
- 内蔵ドライブ全体の調査は、ファイル数が多いと時間がかかる（3,000 件のテストは数秒）。実際の大容量ドライブでの所要時間は未計測
- `packaging/build_windows.bat` が使う `packaging/recsort.spec` はリポジトリにない（.gitignore で除外されている）
- `tools/stress/` のストレス試験は Linux 向けで、今回は実行していない（参照先だけ新しい画面構成に合わせた）
- Web 版の `web/dist/RecSort-PC.zip`（ビルド済みの exe）が `web/companion/Scan.cs` から作られたものかは確認していない
- Web 版の `index.html` をファイルとして開いたときのダウンロード先は外部の公開ページ（`*.chatgpt.site`）で、リポジトリの管理外
