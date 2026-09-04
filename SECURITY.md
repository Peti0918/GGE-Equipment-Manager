# Security notes

## Password handling

GGE Equipment Manager v0.7.0 does not persist passwords.

A password exists only while the user enters it and while the login request is
being authenticated. The GUI clears the password after a successful login,
disconnect, and application exit. The core session keeps only non-secret
connection metadata after authentication.

No application feature writes the password to:

- `accounts.json`
- equipment snapshots
- log files
- the application directory
- Git configuration or GitHub

## What is stored

The app may store these per-Windows-user files under:

`%LOCALAPPDATA%\GGE Equipment Manager\`

- `accounts.json`: remembered usernames only.
- `commander_loadouts\`: equipment snapshots keyed by player ID.

These files are not bundled into the EXE and do not travel when the EXE is
copied to another computer.

## Sharing

Share the generated `GGE Equipment Manager.exe` or a clean release ZIP only.
Do not share your `%LOCALAPPDATA%\GGE Equipment Manager` folder.

Older development builds may have created `profiles.json` with plaintext
passwords. v0.7.0 removes/scrubs known legacy `profiles.json` files when it
migrates their usernames, but old backups or ZIP archives must be deleted or
kept private separately.
