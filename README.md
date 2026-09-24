# drive-organiser

Watches one folder in Google Drive. For every file it finds, it reads the
document, asks Gemini for a descriptive filename and a destination folder,
renames the file, and moves it there. Then it emails you a summary with links.

Runs hourly in a container.

```
_Inbox/                                    Invoices & Receipts/
  Scanned_20180517-1651.pdf        ->        Hugo Boss Purchase Receipt 2018-04-22.pdf
  A-87D3387F-443069414-1.pdf       ->      Household Bills & Services/
                                             Octopus Energy Electricity Bill 2026-07-26.pdf
```

Files inside subfolders of the inbox are processed too. The subfolder itself is
ignored — it is neither moved nor used as a hint about where the file belongs.

## How it decides

Destination folders are listed in `config.toml`, each with a description. **The
description is the control.** Gemini reads it, and the wording decides where
documents land. A vague description pulls in files that do not belong.

Two gates are enforced in code rather than trusted to the model:

- The chosen folder id must be one you configured. An invented id goes to `_Unsorted`.
- Confidence below `min_confidence` (default 0.6) goes to `_Unsorted`.

Together these contain prompt injection. A document instructing the model to
misfile itself can at worst produce a poor filename — it can never reach a folder
you did not configure.

## Requirements

- Python 3.12, [uv](https://docs.astral.sh/uv/)
- Docker (OrbStack, Docker Desktop, or any daemon) for the scheduled job
- A Google Cloud project, a Gemini API key, and a Gmail account with 2FA

## Setup

### 1. Google Cloud

Enable the Drive API on your project:

<https://console.cloud.google.com/apis/library/drive.googleapis.com>

Create a service account. **Grant it no IAM roles** — Drive access comes from
folder sharing, not from IAM, and roles here do nothing.

<https://console.cloud.google.com/iam-admin/serviceaccounts>

On that account, open **Keys → Add key → Create new key → JSON**. Save the file
as `secrets/service-account.json` in this repo. It is git-ignored.

Copy the account's email. It looks like:

```
drive-organiser@your-project-123456.iam.gserviceaccount.com
```

### 2. Drive folders

Create a parent folder holding the inbox, the fallback, and your destinations:

```
Organised/
├── _Inbox/          documents land here
├── _Unsorted/       fallback for anything the model cannot place
├── Invoices & Receipts/
├── Household Bills & Services/
└── ...
```

**Share `Organised/` itself with the service account email, as Editor.** Untick
"Notify people" — a service account has no mailbox.

> Share the parent, not the subfolders. Drive strips permissions inherited from a
> shared folder when a file moves *out* of it. If you share only `_Inbox/`, the
> service account loses access to every file the moment it files one, and every
> move fails.

Create every folder by hand. A service account has no storage quota and cannot
own files, so this job can never create a folder. It also cannot delete: Drive
reports `canDelete: false`, so "never deletes your files" is enforced by Google
rather than by this code.

### 3. Keys

- Gemini API key: <https://aistudio.google.com/apikey>
- Gmail app password: <https://myaccount.google.com/apppasswords> (needs 2FA).
  Sixteen characters. Your normal Google password will not work over SMTP.

### 4. Configure

```bash
cp .env.example .env
```

Fill in `GEMINI_API_KEY`, `GEMINI_MODEL`, `SMTP_APP_PASSWORD`. Set `GEMINI_MODEL`
to a current model — do not copy an old one from a blog post.

Then generate the folder config:

```bash
uv run drive-organiser list-folders
```

It prints paste-ready TOML for every folder the service account can see, with the
ids filled in and the shared parent excluded. Paste it into `config.toml` and
write a description for each folder. Be specific, and say which folder wins where
two could plausibly claim a document:

```toml
[[destinations]]
id          = "1ldIO..."
name        = "Invoices & Receipts"
description = """Receipts and invoices for one-off purchases, order confirmations
and warranties. Use this only when no other folder fits better. A recurring
household bill belongs in Household Bills & Services; a pension or medical
document belongs in its own folder even when it mentions money."""
```

### 5. Check

```bash
uv run drive-organiser check
```

Validates credentials, confirms every configured folder resolves and is writable,
warns about Drive folders missing from `config.toml`, counts the inbox, and logs
in to SMTP. It changes nothing.

## Running

One pass, right now:

```bash
uv run drive-organiser run
```

Hourly, in a container:

```bash
docker compose up -d --build
```

Note that `up -d` runs immediately, then sleeps to the next hour. Restarting the
container is therefore also a run. That is harmless — filed files leave the
inbox, so a second run finds nothing.

```bash
docker compose logs -f
```

## Commands

| Command | Effect |
|---|---|
| `check` | Validate config, folders and SMTP. Writes nothing. |
| `list-folders` | Print paste-ready TOML for visible folders. |
| `run` | File every inbox document, once. |
| `loop` | Run on the hour, forever. The container entrypoint. |

`run` and `loop` accept:

- `--dry-run` — show what would happen and write nothing. For debugging.
- `--no-email` — print the summary instead of emailing it.

## Configuration

`.env`:

| Variable | Meaning |
|---|---|
| `GEMINI_API_KEY` | From AI Studio |
| `GEMINI_MODEL` | Model id. No default — model names change. |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to the JSON key |
| `SMTP_USER`, `SMTP_APP_PASSWORD`, `MAIL_TO` | Gmail app password, not the account password |
| `TZ`, `LOG_LEVEL` | `Europe/London`, `INFO` |

`config.toml`:

| Key | Meaning |
|---|---|
| `[drive]` | `inbox_folder_ids` (a list; every folder in it is watched), `unsorted_folder_id`. The older single `inbox_folder_id` still works. |
| `[[destinations]]` | `id`, `name`, `description` — one block per folder |
| `min_confidence` | Below this, a file goes to `_Unsorted`. Default 0.6. |
| `max_files_per_run` | Cap per run. Default 50. |
| `max_inline_bytes` | Above this, only metadata reaches Gemini. Default 20 MB. |
| `email_on_empty` | Email when the inbox was empty. Default false, or the hourly job becomes noise. |

## What reaches Gemini

| File type | Sent |
|---|---|
| Google Docs | exported as text |
| Google Sheets | exported as CSV (first sheet only) |
| Google Slides | exported as PDF |
| PDF, images, Office files | the bytes |
| Text, Markdown, JSON, CSV | the text, first 20k characters |
| Anything else, or too large | filename, type, size and date only |

File contents leave your machine for the Gemini API. Keep credential documents —
password manager exports, recovery kits — out of the inbox.

## Known limits

- **Names are not repeatable.** Each run calls Gemini afresh, and the model
  samples its output. A `--dry-run` shows the quality to expect, not the names
  you will get. The same document processed twice can be named two ways.
- **No undo.** The summary email is the only record of a run.
- **Duplicates are not detected.** Two copies of one document are filed twice,
  the second with a ` (2)` suffix.
- **Empty subfolders accumulate** in the inbox. The service account cannot delete
  them.
- **The container needs a login session.** OrbStack starts at login, not at boot.
  A rebooted machine sitting at the login window runs nothing.

## Tests

```bash
uv run pytest
```

135 tests, none of which touch the network. Fakes for Drive and Gemini live in
`tests/fakes.py`.

## Layout

| Path | Job |
|---|---|
| `cli.py` | Commands |
| `config.py` | Reads `.env` and `config.toml` |
| `drive.py` | The Drive API |
| `extract.py` | Decides what to send Gemini per file type |
| `gemini.py` | The prompt and the response schema |
| `naming.py` | Sanitising, extensions, collisions |
| `organiser.py` | The per-file pipeline. The only code that writes to Drive. |
| `report.py` | Builds the email |
| `mailer.py` | Sends it |
