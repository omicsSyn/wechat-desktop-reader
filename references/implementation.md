# Implementation and verification

The desktop and local paths are separate. Choose the local path for minimized-window reading; see [local-mode.md](local-mode.md). Choose desktop capture when the requested material is visible in the client but unavailable through the supported local format. Never switch from local reading to visible navigation without an agreed collection period.

## Desktop adapter

The desktop helper uses Windows PowerShell, `System.Drawing`, `PrintWindow` with flag 2, and guarded `PostMessage` calls. It has no physical-mouse, clipboard, message-send, window-activation, or automatic window-restoration command. Posted navigation can still change the visible conversation or open a viewer.

On the exercised Windows Weixin 4.1.15.55 client, UI Automation exposed a Qt main window and `MMUIRenderSubWindowHW`, without useful chat-text controls. Top-level-window capture and posted navigation were usable. This is an observed implementation detail, not a stable client API.

A snapshot stores process, handle, title, geometry, a capture hash, and an optional verified region. Actions reject stale identity, changed pixels, and coordinates outside the verified region. These checks reduce races; they do not make capture-and-click atomic or prove which contact is selected. The agent must inspect the chat heading and each returned image. Unicode search input can be ignored by some controls, so uncertain focus is a stopping condition.

Minimized and hidden-window captures did not provide readable evidence in the exercised environment. Off-screen navigation was not validated and is not included. A successful `PrintWindow` return alone does not prove readable content.

## Local adapter

`local_reader.py` orchestrates read-only process-key discovery, a page-based SQLCipher adapter, exact contact lookup, structural attachment lookup, and a separate read-only window observer. Runtime keys are captured in private parent/child pipes and are not saved or printed by the parent. The source database is never opened for writing. Query results and decoded images are deliberate private output files.

The observer does not activate windows. Its event hooks and samples describe observed state; `ok`, `background_verified`, and `minimized_verified` have different meanings. The VFS checks authenticated database pages and committed WAL frames, then rechecks the source content. This is a guarded read, not an atomic cross-database snapshot.

## Tests

The regression suite creates synthetic encrypted SQLite pages, WAL frames, protobuf fields, images, and observer subprocesses. It covers read-only enforcement, authentication failure, incomplete WAL commits, source changes, wrong attachment identity, image decoding, observer timeouts, cleanup, and incomplete observations. It requires no WeChat account, process key, or private fixture.

No real WeChat client runs in GitHub Actions. Windows UI behavior, client-version compatibility, live synchronization coverage, and attachment availability require separate local verification.

## API and source references

- [Microsoft: SetWinEventHook](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook)
- [Microsoft: PrintWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-printwindow)
- [Microsoft: PostMessageW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-postmessagew)
- [SQLite WAL file format](https://www.sqlite.org/fileformat2.html#walformat)
- [SQLCipher API](https://www.zetetic.net/sqlcipher/sqlcipher-api/)
- Adapted source files, pinned revisions, and retained notices are listed in [THIRD_PARTY.md](../THIRD_PARTY.md).
