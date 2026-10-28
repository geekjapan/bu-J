# Webタスクの比較試験

Google Flights、Wikipedia、ホテル検索を専用Chromeで実行します。既存の合成fixture用Controllerは変更していません。

修正前の30試行は[比較結果](web_benchmark_results.md)を参照してください。失敗原因と修正後の確認は[修正記録](web_failure_fixes.md)に分けて保持します。

## 試験を実行する

Python 3.12、Docker、既存Azure CLIログイン、Microsoft-Decision-1とgpt-4.1-miniの既存deploymentを使用します。推論には利用料が発生します。`.env.example` に従って接続先を設定してください。認証tokenはメモリ内だけで扱います。

```sh
uv sync --locked --extra dev --extra web
docker build --platform linux/amd64 -f verification/chrome/Dockerfile -t bu-j-chrome-verifier:local .
uv run --no-sync --env-file .env python -m benchmarks.web suite --trials 5 --flight-date 2027-09-20
```

両実装を各タスク5回ずつ、ABとBAの順を交互に実行します。各試行は新しい専用profileを作ります。コンテナのread-only root、tmpfs、公開portの127.0.0.1への限定、mountなし、capability削除、所有権とimage digestを起動時に照合します。公開サイトへの通信はChromeのプロキシを使い、許可ドメインと解決したIPを検査します。これはDockerのネットワークを遮断した試験ではありません。

計測開始は初期ページ観測後の最初の判断です。終了はfreshなDONE選択で、ブラウザ起動・初期観測・独立した最終検証・PNG取得は含めません。読み書きのjournalと判断中の待ち時間は含めます。全30試行の成功、失敗、棄権、timeoutを保存します。失敗による短い終了時間を速度向上に数えません。

各試行は最大60操作、120判断HTTPリクエスト、20文字列生成HTTPリクエストです。120秒の期限を操作間で確認し、suiteは180秒で子プロセスを打ち切ります。suite全体では判断360回、文字列生成120回を上限に、各子プロセスへ残量を割り当てます。子プロセスの使用量証跡を失った場合は、それ以降の呼び出しを停止します。これは金額による課金上限ではありません。

## 比較条件

公開資料は [jev-ultrafastの固定版](https://github.com/browser-use/jev-ultrafast/tree/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/docs) を参照します。

| 条件 | 公開測定 | 今回の両実装 |
| --- | --- | --- |
| 判断モデル | TypeSafe jev-1.13.0 | Microsoft-Decision-1 |
| 文字列生成 | inception/mercury-2.5 | Azure gpt-4.1-mini |
| Flights日付 | 2026年9月20日 | ユーザー指定の2027年9月20日 |
| ブラウザ | Chrome 152、既存profile | 同じimageのChrome 156、試行ごとに専用profile |
| 反復 | Flightsは両側3回、他は単独smoke | 各タスク両側5回 |

upstream側は固定版のAgent loopをそのまま保存し、ブラウザとモデルへの接続を共通adapterに差し替えます。両実装ともサイト・controlの許可判定を通します。Microsoft APIがchoiceに2候補以上を要求するため、対象が1件だけのheadには「該当なし」を追加します。upstream側は元のモデル選択の採用方法を維持し、bu-J側はoperationと選択されたtargetそれぞれに0.90、margin 0.20、state support 0.95の基準を適用します。

この比較は同じ条件に揃えたadapter付きupstreamとbu-Jの比較です。公開測定の歴史的な速度比を、そのまま再現したとは扱いません。モデルの校正や採用基準の差も結果に影響します。

## 結果を検証する

起動時に `.audit/web-benchmark/<run-id>` を表示します。`conditions.json` はsource hash、入力、環境を、`runs.json` は全試行を、`summary.json` は成功件数と成功時の中央値・範囲を記録します。各試行の `result.json`、実API応答、PNG、コンテナ起動・削除の記録を保持します。bu-Jでは `journal.sqlite3` に操作前のDISPATCHEDを永続化し、効果が観測できない場合はUNKNOWN_EFFECTで停止します。

DONEだけでは合格にしません。Flightsは経路、日付・年・曜日、検索結果、1人、economyを照合します。Wikipediaは正確な記事URLと記事本文を照合し、URLだけが一致するエラーページを拒否します。ホテルはCasa Floraの詳細と3条件を確認します。航空便選択・予約、ログイン、Wikipedia編集は許可しません。

個別のbu-Jタスクは、suiteが作る有効な `manifest.json` を指定して `decision-browser-web` から実行できます。任意URLや個人Chromeを自動探索する設定はありません。

## 保証の範囲

新Web runtimeの永続化と復旧は、実SQLite・子プロセス強制終了・故障注入の試験で確認します。既存TLA+モデルの保証は従来のfixture経路に限られ、新Web runtimeの形式証明ではありません。iframe、shadow DOM、canvas、アップロードは対象外です。試行の少なさと実サイト・ネットワークの変動があるため、成功率や時間の小さな差から一般的な優劣を断定しません。
