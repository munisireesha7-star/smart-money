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

## Deploy to Vercel

Vercel functions do not provide durable local disk storage. This project uses PostgreSQL whenever `DATABASE_URL` is set, and stores uploaded photos as database binary data so they persist with the account records. Do not deploy real accounts using the local SQLite database.

1. Push this project to GitHub and sign in to [Vercel](https://vercel.com/).
2. In Vercel, create a PostgreSQL database through the Neon integration (or use an existing PostgreSQL database). Copy its pooled connection string.
3. Import `munisireesha7-star/smart-money` as a Vercel project. Vercel detects `vercel.json` and installs `requirements.txt`.
4. In **Project Settings → Environment Variables**, add:
   - `DATABASE_URL`: the PostgreSQL connection string. Keep it secret.
   - `SMART_MONEY_SECRET_KEY`: a long random secret. Generate one locally with `py -c "import secrets; print(secrets.token_hex(32))"`; do not commit it.
   - `SMART_MONEY_COOKIE_SECURE`: `true`.
5. Apply the environment variables to Production and Preview as appropriate, then redeploy. The app creates its tables on startup.
6. Open the deployment URL and create an account. Verify registration, login, record creation, photos, alerts, and reports on the hosted instance before inviting users.

`DATABASE_URL` and `SMART_MONEY_SECRET_KEY` must be set before the app starts on Vercel. Uploaded photos are limited to 4 MB to fit serverless request limits. Do not add database URLs or secrets to GitHub. Local SQLite data is not automatically migrated to PostgreSQL.

## Existing databases

On startup, the app adds nullable account ownership columns to an existing database. Old rows without an owner remain unowned and are not shown to newly registered accounts. They are not automatically assigned to the first account.

## Tests

Run all project tests from the project folder:

```powershell
py -m unittest discover -v
```
