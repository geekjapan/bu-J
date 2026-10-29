# 汎用ブラウザを検証する

`generic` extraとREADMEのDocker base imageを用意し、リポジトリ直下で実行する。

```sh
uv run --no-sync python verification/generic_browser/verify.py --output .audit/general-verification-unique
```

専用Chromeでフォーム、native入力・キー・選択、同一originと別originのiframe、open Shadow DOM、タブ、履歴、再読込、スクロール、ファイル転送を操作する。`proof.json`の`passed`、サーバー側カウンター、ファイルhash、PNGと`chrome/cleanup.json`の`absent`を確認する。新しい出力先を使い、失敗した証跡は残す。

遷移制限と確認ダイアログの異常系は、別の専用Chromeで検証する。

```sh
uv run --no-sync python verification/generic_browser/check_boundaries.py --output .audit/general-boundaries-unique
```

禁止先へのredirect・popupのHTTP到達が0であることと、confirmを自動承認しないことを確認する。入力応答timeoutは`UNKNOWN_EFFECT`として保存し、再送しない。

このコマンドはモデルを使わず、サイト固有操作を含む検証ハーネスである。汎用plannerの受入は、許可済みの実Azure APIでREADMEの`decision-browser-general`サンプルを別途実行する。`result.json`のDecision1・helperのHTTP状態、`data`、独立acceptance、journalとcleanupを確認する。Mockやこの操作ハーネスだけで自律タスク成功を宣言しない。

`pytest -q`は不正候補、origin、ファイル、期限、保存順序、例外と再実行拒否の単体試験を含む。従来TLA+の証明は汎用runtimeを対象としない。全Webサイトやbrowser-use全機能の互換試験ではない。

Windows CIのnative Chrome検証は`uv run --no-sync python -m verification.windows_browser --output .audit/windows-native-unique`で実行する。専用の一時profileとfixtureだけを使い、入力・キー・frame・Shadow DOM・タブとプロセス終了を検証する。Docker Desktop・LinuxコンテナのWindows上の受入とは別の試験で、ファイル転送やAzure実接続を証明しない。
