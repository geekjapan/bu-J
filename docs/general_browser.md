# 普段使っているブラウザーでWebタスクを実行する

Python 3.12、uv、起動中のChrome、既存のAzure CLIログインを使う。Microsoft-Decision-1とgpt-4.1-miniの接続先は[README](../README.md)に従って設定する。Windowsでもnative PythonとPowerShellで同じコマンドを使う。

## 接続する

起動中のChromeで`chrome://inspect/#remote-debugging`を開き、remote debuggingを有効にする。接続時の確認はChromeの画面で承認する。[Chromeの設定手順](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session)を参照する。通常のプロファイルをそのまま使い、別プロファイルの起動やDockerの準備は不要。

```sh
uv sync --locked --extra generic --python 3.12
uv run --no-sync decision-browser-general --list-tabs
```

`--list-tabs`はタブのID、URL、タイトルだけを表示する。タスク、出力先、Azure認証は不要で、ページ本文の取得、タブの作成、遷移は行わない。

接続先は`--cdp-url`、`BU_CDP_WS`、`BU_CDP_URL`の順に使う。いずれも未指定ならBrowser Harnessが通常のChromeプロファイルの`DevToolsActivePort`から検出する。`--cdp-url`にはbrowser WebSocket URLかHTTPのCDP endpointを指定できる。HTTP endpointは`/json/version`からWebSocket URLを解決する。平文の`http`と`ws`はloopback接続だけを許可する。自動検出に失敗した場合はChromeの設定を確認する。別ブラウザーやDockerへの自動切替は行わない。

```sh
uv run --no-sync decision-browser-general --cdp-url http://127.0.0.1:9222 --list-tabs
```

この例のポートは、実際にChromeが公開しているendpointへ置き換える。

## タスクを実行する

タスクJSONの`url`と`goal`を編集して実行する。`--output`には新しいディレクトリを指定する。

```sh
uv run --no-sync --env-file .env decision-browser-general --task examples/general-task.json --output .audit/example-general
```

既定では接続先ブラウザーの同じプロファイルに作業タブを作る。タブ作成と開始URLへの遷移は、最初のNavigate操作をjournalへ保存した後に行う。既存の別タブは操作対象に含めない。

既存タブで作業する場合は`--list-tabs`のIDを指定する。

```sh
uv run --no-sync --env-file .env decision-browser-general --task task.json --tab-id TARGET_ID --output .audit/existing-tab
```

`--tab-id`を指定すると開始URLへ自動遷移せず、現在のページから作業を始める。接続時に未送信フォームやsessionStorageを初期化しない。タスクが必要とする入力や遷移は、その後の操作で実行される。既存ログイン状態を利用できるため、対象タブと目的を確認して指示する。無関係なタブの本文をモデルへ送らない。

終了時はCDP接続を切断し、ブラウザーとタブを残す。明示的な`close_tab`操作でも閉じられるのはタスクが作成したタブだけ。借用した既存タブを閉じたり、Chromeを終了・再起動したりしない。cookie削除、viewport変更、ブラウザー全体のダウンロード先変更は行わない。

## タスクとファイルを指定する

`allowed_origins`にはscheme・host・portが一致する遷移先を指定する。省略時は開始URLのoriginだけを許可する。必要な外部サイトを先に追加する。通常のHTTP(S) URL、localhost、社内ホスト、private IP、IPv6と任意の有効なポートを指定できる。HTTPの80番とHTTPSの443番はorigin表記で省略する。URLはASCII表記とし、認証情報を含むURLや`file:`などのWeb以外のschemeは拒否する。

`allowed_origins`はControllerの操作先と、接続済みタブのDocument遷移を制限する。実ブラウザーでは既存ページのスクリプトや通信も動作し続ける。ページが開いたpopupは親タブIDを確認して操作対象に取り込むため、popupの最初の通信が監視開始より先に発生する場合がある。ブラウザー全体の通信隔離を保証する設定ではない。

ブラウザーはリンク、入力、選択、キー、縦横スクロール、履歴、再読込、タブ、iframe、open Shadow DOMを扱う。任意のJavaScript・shell・CSS selectorをモデルから実行しない。確認ダイアログを自動承認せず、CAPTCHA回避も行わない。操作はフォーム送信を含み、クリック名だけで読み取り専用とは判断できない。目的に必要な操作だけを指示する。

`uploads`は任意のトークンと明示したローカル通常ファイルの対応表。承認したファイルだけを指定する。実行時に検証したコピーを出力先の`uploads/`へ保存し、そのローカルパスをChromeに渡す。WindowsのパスはJSON内で`C:/Users/name/specification.pdf`のように指定できる。symlinkとreparse pointを拒否するため、クラウドのプレースホルダーは通常のローカルファイルへコピーする。

ダウンロードの実ファイルも回収する場合は、Chromeで設定している保存先を`--downloads-dir`に指定する。

```sh
uv run --no-sync --env-file .env decision-browser-general --task task.json --downloads-dir /path/to/Downloads --output .audit/with-files
```

タスクで観測した完了済みダウンロードのCDP metadataに、正確な保存済みパス`filePath`がある場合だけ回収する。指定した`--downloads-dir`の直下にある通常ファイルを`downloads/`へコピーする。`suggestedFilename`だけでは既存ファイルと照合せず、無関係なファイルは回収しない。保存先未指定、正確な保存済みパス未取得、未完了の場合はmetadataだけを記録し、ファイル取得成功とは扱わない。Chromeの保存先設定は変更しない。1ファイル10 MiB、ダウンロード全体40 MiBを上限とする。保存名はOS間で衝突しないhash名とし、元の名前を結果の`name`へ残す。

`output_schema`には参照を含まないJSON Schemaを指定できる。抽出データはスキーマ検証し、結果の`extracts`へ保存する。長いページは100,000文字ずつ抽出する。`vision: true`では観測画像もAzureへ送る。対象タブの本文、入力値、画像は外部モデルへの送信対象になるため、タスクで扱う情報の範囲を確認する。

`max_steps`は既定20、最大40。`timeout_seconds`は既定300、最大600。Microsoft-Decision-1への要求は最大`max_steps`回、gpt-4.1-miniによる候補生成と抽出は合計最大20回。既存の有料APIを利用し、新しいモデルやリソースは作成しない。

## 結果を確認する

`result.json`、screenshots、extracts、ファイルのreceipt、SQLite journalを確認する。

- `VERIFIED`は、モデルの完了報告後に`acceptance.url`と`acceptance.text`の指定条件が新しい観測と一致した状態。
- `REPORTED`は、独立した`acceptance`がない完了報告。目的全体の正しさを保証しない。
- `ACKNOWLEDGED`はSQLite内の操作への応答であり、サイトの効果や目的達成の証明ではない。
- `UNKNOWN_EFFECT`は送信後の例外・中断。同じ操作を自動再送しない。サイトの状態を照合し、新しいIDやjournal削除で不明な操作を回避しない。

[汎用ブラウザー検証](../.cursor/skills/verify-decision-browser/features/general.md)で実ブラウザーの確認範囲を参照できる。全サイトでの成功やbrowser-use全機能との完全互換は保証しない。[従来の固定3タスク](web_benchmark.md)は別のCLIと任意のDocker検証環境を使う。
