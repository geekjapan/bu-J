# 普段使っているブラウザーへの接続を検証する

`generic` extraと起動中のChromeを用意する。通常の接続は[タスクガイド](../../../../docs/general_browser.md)に従い、Chrome側でremote debuggingを有効にする。Dockerや別プロファイルは汎用CLIの前提ではない。

## 既存ブラウザーで確認する

リポジトリ直下で実行する。

```sh
uv run --no-sync decision-browser-general --help
uv run --no-sync decision-browser-general --list-tabs
```

`--list-tabs`がID、URL、タイトルだけを返すことを確認する。タスク、出力先、Azure認証は不要。確認前後で新しいタブや遷移が発生しないことも確認する。接続先を指定する場合は`--cdp-url`を使う。未指定時は環境変数、続いてBrowser Harnessの通常プロファイル検出を使う。接続失敗を別ブラウザー起動で回避しない。

利用者が指定したfixtureタブに未送信フォームとsessionStorageを用意する。別のfixtureタブも開き、URL、フォーム値、sessionStorage、viewportを記録する。fixture以外のタブ本文は取得しない。

既存タブを使うCLI試験では`--tab-id TARGET_ID`を指定する。接続時にタスクの開始URLへ遷移せず、借用タブの未送信状態を保持することを確認する。操作後は目的に対応したサーバー側カウンターとjournalを照合する。終了後もChromeと借用タブが生きており、別タブの記録値が変わらないことを確認する。

既定の新規作業タブも別途試す。最初のNavigateをjournalへ保存した後に同じブラウザーでタブを作り、開始URLへ1回だけ遷移することを確認する。切断後も結果のタブを残す。タスクが作ったタブだけが`close_tab`の対象になり、借用タブは閉じられないことを確認する。

アップロードは承認したローカルファイルを使い、出力先のコピー、receipt、サーバーで受けたhashを照合する。ダウンロード試験はChromeの実際の保存先を`--downloads-dir`へ指定し、完了済みCDP metadataの正確な保存済み`filePath`が指定ディレクトリ直下にある場合だけ回収することを確認する。`suggestedFilename`が既存ファイルと同名でも、正確な保存済みパスがなければmetadataだけを記録する。無関係なダウンロードは回収しない。保存先や正確なパスの未取得時にファイル取得成功を宣言しない。

実Azure plannerの受入には、別途許可された有料推論で`decision-browser-general`を実行する。`result.json`のDecision1とhelperのHTTP状態、`data`、独立acceptance、journal、ファイルreceiptを確認する。Mockやモデル未使用の操作だけで自律タスク成功を宣言しない。

macOSの通常プロファイル接続と実Azure推論の受入証跡は`.audit/real-browser/native-read/proof.json`にある。既定の新規タブと借用タブの両方が`VERIFIED`で、cookie再利用、未送信状態・sessionStorage・viewport・無関係なタブの保持、切断後のブラウザー生存を確認した。この証跡は全サイトの成功やダウンロード回収を証明しない。

## Windowsのnative検証

```sh
uv run --no-sync python -m verification.windows_browser --output .audit/windows-native-unique
```

この自動試験はCI用の一時profileとfixtureを使う。実CLIの`--list-tabs`、入力・キー・frame・Shadow DOM・タブ操作、借用タブと無関係なタブのURL・未送信フォーム・sessionStorage・viewport保持、切断後のブラウザーとタブの生存を確認する。`proof.json`と各証跡を確認する。CIの一時profileは試験用であり、通常利用の接続手順ではない。個人プロファイルの自動検出とAzure実接続の受入は別に確認する。

## 任意の従来Docker試験

従来のfixture操作試験はDockerを使う。実行する場合だけ[Chrome検証の準備](../../../../verification/chrome/README.md)に従う。

```sh
uv run --no-sync python verification/generic_browser/verify.py --output .audit/general-verification-unique
uv run --no-sync python verification/generic_browser/check_boundaries.py --output .audit/general-boundaries-unique
```

フォーム、iframe、open Shadow DOM、履歴、スクロール、タブ、ファイルと遷移制限を確認する歴史的な操作試験であり、通常ブラウザー接続や借用タブ保持の証明にはしない。`proof.json`、サーバー側カウンター、hash、PNG、コンテナのcleanupを確認する。失敗した証跡も残す。

`pytest -q`は不正候補、origin、ファイル、期限、保存順序、例外と再実行拒否の単体試験を含む。従来TLA+の証明は汎用runtimeを対象としない。全Webサイトやbrowser-use全機能の互換試験ではない。
