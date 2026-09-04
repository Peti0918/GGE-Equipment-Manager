# GGE Equipment Manager v0.7.0

A Windows-focused commander equipment helper for Goodgame Empire.

The app connects once and keeps the same live game session open while you save,
unequip, or restore commander equipment.

## Security model

This release deliberately **does not save passwords**.

- Passwords are used only for the current login attempt.
- The password is not written to JSON, config files, logs, snapshots, or the app directory.
- After a successful login, the password field is cleared.
- The password field is also cleared on disconnect and exit.
- Successful logins remember only the username for the account dropdown.
- Selecting a remembered username never fills a password; the user must type it again.

Remembered usernames are stored per Windows user at:

`%LOCALAPPDATA%\GGE Equipment Manager\accounts.json`

Equipment snapshots are stored separately at:

`%LOCALAPPDATA%\GGE Equipment Manager\commander_loadouts\`

This means copying or sending the application/executable does **not** copy saved
usernames, equipment snapshots, or passwords with it.

### Upgrade from V4/V5/V6

Older GUI builds used `profiles.json` and could contain plaintext passwords.
On first launch, v0.7.0 imports the usernames from old `profiles.json` files and
then removes the old profile file. If removal is not possible, the app blanks
its password fields as a fallback.

Old unrelated `config.json` files are not read or modified by this release.

If you have old ZIP backups of V4/V5/V6, they are outside the control of this
app and may still contain a previously saved `profiles.json`. Do not distribute
those old backups.

## Features

- One persistent WebSocket login session; no reconnect between equipment actions.
- Multiple remembered usernames with dropdown/autocomplete.
- No password persistence.
- Save commander equipment snapshot.
- Unequip equipment only for commanders represented in the saved snapshot.
- Restore equipment to saved commander positions.
- Moving commanders are skipped safely.
- Live equipment storage scanner.
- Unequip is blocked when storage does not have enough free slots.
- Live log and progress bar.
- Resizable dark-blue GUI with persistent Live Log panel.

## Equipment storage

The app reads the game state as follows:

- `gbd -> esl -> TE`: total equipment storage capacity.
- `gbd -> esl -> E`: free equipment storage slots.
- `gei -> I`: current equipment inventory; `len(I)` is the used slot count.

Before Unequip All, the app performs another live `gei` refresh and blocks the
action if the required items would not fit.

## Run from Python

Requirements:

- Python 3.11+ recommended
- Windows 10/11

Install dependencies:

```bat
py -m pip install -r requirements.txt
```

Run:

```bat
py gui.py
```

## Build a Windows EXE

Run:

```bat
build_windows.bat
```

The script installs PyInstaller if needed and creates:

`dist\GGE Equipment Manager.exe`

For sharing with teammates, send **only the generated EXE** or a clean release
ZIP containing it. Do not send your `%LOCALAPPDATA%\GGE Equipment Manager`
folder.

## GitHub build

The repository includes `.github/workflows/build-windows.yml`.
You can run the workflow manually from the GitHub Actions tab, or push a tag
such as `v0.7.0`. The workflow produces a Windows EXE artifact.

## Local files that must never be committed

The included `.gitignore` excludes legacy credentials, local account data,
snapshots, logs, dumps, and build output.
