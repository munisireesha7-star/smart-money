# Smart Money – Home Money Loss Detection

A responsive Flask and SQLite app for tracking home cash, comparing a manual cash count with the recorded balance, and reviewing possible discrepancies. A discrepancy is only an observation to review; it does not establish theft or identify a person.

## Run locally in VS Code on Windows

Open this project folder in VS Code. Open **Terminal → New Terminal** and run these commands in PowerShell from the project folder:

```powershell
py --version
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
[Environment]::SetEnvironmentVariable("SMART_MONEY_SECRET_KEY", (py -c "import secrets; print(secrets.token_hex(32))"), "User")
```

If PowerShell blocks virtual-environment activation, run this once in that terminal, then activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

The secret is saved in your Windows user environment so it stays the same when you reopen VS Code. Keep it private. The command generates a new secret each time you run it, so only run it once unless you intend to invalidate existing login sessions. Close that terminal and open a new one so Windows loads the setting. Then run:

```powershell
.\.venv\Scripts\Activate.ps1
py app.py
```

Open `http://127.0.0.1:5000` and create an account. The SQLite database and uploaded cash photos are stored locally by the app.

For an HTTPS deployment, set `SMART_MONEY_COOKIE_SECURE=true` so browsers send the session cookie only over HTTPS. Leave it unset for local HTTP development. Do not enable `SMART_MONEY_DEBUG` in a deployment.

Email delivery is not configured; alerts and test notifications stay inside the app. Never enter a bank PIN, OTP, or payment password.

## Existing databases

On startup, the app adds nullable account ownership columns to an existing database. Old rows without an owner remain unowned and are not shown to newly registered accounts. They are not automatically assigned to the first account.

## Tests

Run all project tests from the project folder:

```powershell
py -m unittest discover -v
```
