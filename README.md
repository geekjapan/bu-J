# bu-J

A Python browser controller using Microsoft-Decision-1. Give it a URL and a natural-language goal to work in your running Chrome, with your existing profile and sign-in session.

Microsoft-Decision-1でブラウザーを操作するPython製Controllerです。URLと自然文の目的を渡し、普段使っているChromeのプロファイルとログイン状態で作業します。

## Install / 導入

Requires Python 3.12, uv, Chrome, and access to existing Azure deployments. Run from the checkout. Windows uses the same commands in PowerShell.

Python 3.12、uv、Chromeと、既存Azure deploymentへの推論権限が必要です。checkoutの直下で実行します。WindowsでもPowerShellで同じコマンドを使います。

```sh
uv sync --locked --extra generic --python 3.12
uv run --no-sync decision-browser-general --help
```

Sign in with Azure CLI. Copy [.env.example](.env.example) to `.env` and set `AZURE_ENDPOINT` and `AZURE_DEPLOYMENT_NAME` for Microsoft-Decision-1, plus `AZURE_TEXT_ENDPOINT` and `AZURE_TEXT_DEPLOYMENT` for an existing gpt-4.1-mini deployment. Task runs call these paid APIs.

Azure CLIでログインします。[.env.example](.env.example)を`.env`へコピーし、Microsoft-Decision-1の`AZURE_ENDPOINT`と`AZURE_DEPLOYMENT_NAME`、既存gpt-4.1-miniの`AZURE_TEXT_ENDPOINT`と`AZURE_TEXT_DEPLOYMENT`を設定します。タスク実行時には有料APIを呼びます。

```sh
az login
```

In your running Chrome, enable remote debugging at `chrome://inspect/#remote-debugging` and approve Chrome's connection prompt. See [Chrome's setup instructions](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session). Edit the sample task's URL and goal, then run it. A working tab opens in that browser and remains open after completion.

起動中のChromeで`chrome://inspect/#remote-debugging`からremote debuggingを有効にし、Chromeの接続確認を承認します。[Chromeの設定手順](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session)も参照してください。サンプルのURLと目的を編集して実行します。同じブラウザー内に作業タブが開き、終了後も残ります。

```sh
uv run --no-sync --env-file .env decision-browser-general --task examples/general-task.json --output .audit/example-general
```

Use a new output directory for each run. See [the browser task guide](docs/general_browser.md) for existing-tab selection, connection discovery, local files, and result checks. Docker is only optional infrastructure for the [historical fixed benchmarks](docs/web_benchmark.md).

出力先は毎回新しいディレクトリにします。既存タブの選択、接続先の検出、ローカルファイル、結果の確認は[タスクガイド](docs/general_browser.md)を参照してください。Dockerは[従来の固定ベンチマーク](docs/web_benchmark.md)用の任意の環境です。

## Install the skill / Skillの導入

Requires Node.js 22.20+ with npx. Select the target agent, start a new session in the checkout, and ask it to use the `bu-j` skill.

Node.js 22.20以降とnpxが必要です。対象エージェントを選択し、checkoutで新しいセッションを開いて「bu-j Skillを使って」と依頼します。

```sh
npx skills add geekjapan/bu-J --skill bu-j --global
npx skills list --global
```

For a project-only install, omit `--global`. The skill provides agent instructions; install the Python runtime and configure Chrome and Azure separately as above.

プロジェクト内だけで使う場合は`--global`を省きます。Skillはエージェントへの手順です。Python本体と依存、Chrome、Azureは上の手順で別途設定します。
