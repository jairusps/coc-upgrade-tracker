# Clash of Clans Personal Progress Tracker

A single-account Clash of Clans progress tracker designed to run on GitHub Actions.

## Data source

The tracker uses the ClashKing public proxy endpoint:

`https://proxy.clashk.ing/v1/players/{encoded_player_tag}`

The player endpoint was selected because it matches the working endpoint/JSON response used during setup.

This project does **not** use:

- a Clash of Clans developer API token
- a Clash of Clans developer email/password
- Discord
- multiple-account support

## What it tracks

The tracker compares successive successful player snapshots and can detect:

- Home Village troop level increases
- Builder Base troop level increases
- Hero level increases
- Hero Equipment level increases
- Spell level increases
- Siege Machine level increases
- Items becoming max level
- Town Hall / Builder Hall changes
- League changes
- Builder Base League changes
- Clan changes
- Clan role changes
- War preference changes
- Trophy changes
- Builder Base trophy changes
- War-star changes
- Donation changes
- Clan Capital contribution changes
- Achievement progress changes

The email can also contain:

- player summary
- upgrade progress summary
- maxed-item summary
- achievement summary

## Important behavior

The first successful run creates `state.json` as the baseline.

It does **not** send an email on that first run.

Later runs compare the newly fetched data with the saved state.

The state is saved only after a successful API fetch. Therefore, an API outage does not overwrite the previous good state.

## GitHub Secrets

Create these repository secrets:

| Secret | Value |
|---|---|
| `PLAYER_TAG` | Your Clash of Clans player tag |
| `GMAIL_ADDRESS` | Gmail address that sends and receives the alert |
| `GMAIL_APP_PASSWORD` | 16-character Gmail App Password |

Do not put the Gmail App Password in the repository.

## Gmail App Password

Use a Google App Password, not your normal Gmail password.

For the app name, for example:

`Clash of Clans Tracker`

## Repository permissions

Go to:

Settings → Actions → General → Workflow permissions

Select:

`Read and write permissions`

The workflow also declares:

```yaml
permissions:
  contents: write
```

## Files

```text
coc-upgrade-tracker/
├── .github/
│   └── workflows/
│       └── tracker.yml
├── config.example.json
├── .gitignore
├── requirements.txt
├── tracker.py
└── README.md
```

`state.json` is created automatically after the first successful run.

## Optional configuration

Copy:

`config.example.json`

to:

`config.json`

All values shown in the example are enabled by default.

The configuration file is optional. If it is absent, the same defaults are used.

## Schedule

The workflow runs once per hour at minute `00` UTC.

GitHub Actions scheduled workflows can sometimes start later than the exact cron time. Manual execution is available through:

Actions → Clash of Clans Personal Tracker → Run workflow

## Troubleshooting

### Player not found / HTTP 404

Verify that `PLAYER_TAG` contains the correct player tag.

The tracker automatically URL-encodes `#`, so the secret can contain either:

`#LL9YPR90P`

or:

`LL9YPR90P`

Do not manually store `%23LL9YPR90P` in the secret.

### Gmail authentication failed

Make sure:

1. 2-Step Verification is enabled on the Google account.
2. The value in `GMAIL_APP_PASSWORD` is the generated App Password.
3. You are not using the normal Gmail password.

### `state.json` missing

A first successful run creates it.

If the tracker fails before the API fetch succeeds, no state file is intentionally created.

### `load_json is not defined`

This project defines all JSON helper functions before configuration loading, so the earlier initialization-order bug is removed.

## Security

Never commit:

- Gmail App Passwords
- `.env` files
- private credentials

`state.json` contains player progress data. If you do not want it public, keep the repository private.
