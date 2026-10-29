# 任意のWebタスクを実行する

Python 3.12、uv、Docker、既存のAzure CLIログインを使う。READMEのChrome imageと、Microsoft-Decision-1・gpt-4.1-miniの設定が必要。

```sh
uv sync --locked --extra generic --python 3.12
uv run --no-sync --env-file .env decision-browser-general \
  --task examples/general-task.json --launch --output .audit/example-general
```

`--output`には新しいディレクトリを指定する。`--launch`は専用Chromeを起動し、終了時に削除する。`--manifest`では汎用モードの所有権確認済みChromeへ接続する。個人Chromeへの接続は行わない。

タスクJSONには`url`と自然文の`goal`を指定する。`allowed_origins`はscheme・host・portが一致する遷移先で、省略時は開始URLのoriginだけを許可する。外部サイトへの移動が必要なら先に追加する。公開HTTPSを利用し、内部IP宛の接続をプロキシで拒否する。HTTPの例外は専用fixtureの`127.0.0.1:8766`と`localhost:8766`だけ。

ブラウザはリンク、入力、選択、キー、縦横スクロール、履歴、再読込、タブ、iframe、open Shadow DOMを扱う。`uploads`は任意のトークンと明示したローカル通常ファイルの対応表。`upload`はそのトークンだけを使い、ファイルをコンテナ内へ転送する。ダウンロードは`downloads/`へ回収する。1ファイル10 MiB、ダウンロード全体40 MiBを上限とする。

`output_schema`には参照を含まないJSON Schemaを指定できる。抽出データはスキーマ検証し、結果の`extracts`へ保存する。長いページは100,000文字ずつ抽出する。`vision: true`で観測画像もAzureのplannerへ送る。画像と観測本文、入力値は外部モデルへの送信対象になるため、公開・合成データを使う。

`max_steps`は既定20、最大40。`timeout_seconds`は既定300、最大600。モデル要求はDecisionが最大`max_steps`回、planner・抽出の合計が最大20回。既存の有料APIを利用する。新しいモデルやリソースを作成しない。

GPT-4.1-miniが型付き候補を生成し、Microsoft-Decision-1が候補から次の1操作を選ぶ。候補・確率・鮮度・origin・ファイルをコードで検査する。任意のJavaScript・shell・CSS selectorをモデルから実行しない。操作はフォーム送信を含み、クリック名から読み取り専用とは判断できない。明示した目的に必要な操作だけを指示する。

`result.json`の状態を確認する。

- `VERIFIED`は、モデルの完了報告後に`acceptance.url`と`acceptance.text`の指定条件が新しい観測と一致した状態。
- `REPORTED`は、独立した`acceptance`がない完了報告。目的全体の正しさを保証しない。
- `ACKNOWLEDGED`はSQLite内の操作への応答であり、サイトの効果や目的達成の証明ではない。
- `UNKNOWN_EFFECT`は送信後の例外・中断。同じ操作を自動再送しない。同じ`task-id`を再使用できない。新しいIDやjournal削除で不明な操作を回避しない。

確認ダイアログの自動承認、個人セッション、CAPTCHA回避、任意コード実行は対象外。全サイトでの成功やbrowser-use全機能との完全互換は保証しない。固定3タスクは従来の`decision-browser-web`と制限を維持する。

実Chromeの再検証は[汎用ブラウザ検証](../.cursor/skills/verify-decision-browser/features/general.md)を参照する。
