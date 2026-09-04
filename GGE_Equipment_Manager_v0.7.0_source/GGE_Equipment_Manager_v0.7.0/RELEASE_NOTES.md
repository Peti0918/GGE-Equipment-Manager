# v0.7.0 - Shareable app build

## Security changes

- Password persistence removed completely.
- Remembered accounts now store usernames only.
- Password field is cleared after successful login, disconnect, and exit.
- Core session no longer retains the password after authentication.
- Old app `profiles.json` password stores are migrated to usernames and removed/scrubbed.
- Runtime user data moved outside the application folder to `%LOCALAPPDATA%`.

## Distribution changes

- Added one-click `build_windows.bat` for PyInstaller.
- Added GitHub Actions Windows EXE build workflow.
- Added release-focused README and `.gitignore`.

## Existing functionality retained

- Persistent live game session.
- Equipment save / unequip / restore.
- Moving commander safety.
- Equipment storage capacity preflight.
- Live progress/log UI and resize-safe layout.
