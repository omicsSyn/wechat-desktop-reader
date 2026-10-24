# Local background reading

Version 0.2.1 provides a deliberately scoped read-only adapter for individual contacts. Group chats are not supported; a matched `@chatroom` username is rejected before message lookup. It runs under WSL Python; a small Windows Python helper reads the logged-in Weixin process. It opens source database files read-only, decrypts requested pages in memory, and exports only the selected conversation's requested records and images. It makes no network calls, sends no messages, and does not change windows or source files.

## Authorization and inputs

Obtain authorization for the explicit local account directory, process key reading, named contact, and requested attachments. Reuse authorization already given within the same scope; approval for one account/contact does not authorize others. Shared database pages can contain other records; do not query or export unrelated conversations. Do not persist account identifiers, patient information, chats, or keys in this skill or general memory.

Check `reader.py status` for the current Weixin PID, main window handle, minimization state, and version. Determine the exact account directory from the user's supplied path or local configuration; do not auto-select another account when validation fails. Confirm that a working Windows Python interpreter is available. No package-wide `wechatauto` installation or import is needed.

Dependencies in the exercised environment: WSL Python 3.12, APSW 3.43, cryptography, Pillow, system libzstd, ffmpeg, and Windows Python 3.11. The database adapter uses `os.pread` and is intended to run under WSL, not native Windows Python. The Windows helper uses the standard library only. Do not install software or services merely to test a missing dependency without considering the current authorization.

Use the parent entrypoint, with values obtained from the current task:

```bash
python3 <skill>/scripts/local_reader.py \
  --account-dir /mnt/d/<authorized-account-directory> \
  --account <exact-logged-in-account-id> \
  --contact '<exact-contact-nickname-or-remark>' \
  --pid <current-Weixin-pid> --hwnd <current-main-window-handle> \
  --windows-python /mnt/d/<python-directory>/python.exe \
  --limit 30 --images 8 \
  --require-minimized \
  --output /mnt/f/<private-task-output>/<new-run-directory>
```

The output directory must not exist and must be outside both the source account and this skill repository. `--limit` selects recent message records across all supported message shards, up to 100. `--images` separately selects recent ordinary image messages, up to 20; these may predate the displayed text window. The runner currently requires at least one requested image slot. Windows Python must be supplied explicitly through `--windows-python`; no machine-specific interpreter path is assumed. It resolves senders using the message's own shard and preserves shard/local/server IDs. A unique exact contact match is required. Other message types are represented by metadata, not silently treated as decoded text or images. A normal text record with `text=null` failed decoding and must be reported as unreadable, not counted as fully read message text.

`--require-minimized` requires WeChat already minimized and not foreground before reading. Without this flag, it can read while WeChat is behind another application. Do not require the user to stop using unrelated software. If the user restores WeChat while reading, report what the observer actually found; do not mistake a user-driven window change for a reader-induced activation. No observer is a permanent background service.

## Keys, source integrity, and coverage

Never execute `_windows_keys.py` directly through a model-visible tool or terminal log. Its stdout contains private key IPC data. Only `local_reader.py` starts it with captured pipes and consumes the result internally. Keys are not passed on command lines, written to key files/logs, or uploaded. They exist in parent/helper process memory and local pipes during the run. This does not claim physical memory erasure or control over operating-system paging/crash dumps.

The account identity must match the requested process and account path. The helper scans Config.Cipher objects and accepts only keys passing each requested database's first-page HMAC. The old cfg master-key derivation is not trusted on the tested 4.1.15.55 client. The cfg value used for image decryption is independently checked through image decoding. Missing database keys stop the scoped query rather than silently losing shards. Only `contact/contact.db` and `message/message_N.db` are accepted; no all-account fallback, bulk plaintext database export, or persistent key cache exists.

The adapter supports 4096-byte SQLCipher 4 pages, 80 reserved bytes, and validated 32-byte raw keys. It checks the HMAC of every page it returns to SQLite. It snapshots at most 64 MiB of encrypted WAL, validates rolling checksums/generation salts, and overlays only committed frames. It does not recover rollback journals or support other cipher layouts.

It rechecks actual source ciphertext pages plus WAL header, commit/used frames, and scan boundary, including when query results are consumed and the context exits. Source changes cause the entire affected result to be discarded; the parent retries once. The result explicitly states `snapshot_guarantee=guarded_read_not_atomic`: content rechecks are not a cooperative SQLite read lock or an atomic snapshot across shards. Do not call these checks full-database integrity validation.

A reset/preallocated WAL can contain no current-generation committed frames. `wal_view=base_db_only_no_committed_wal` and the tail reason must remain visible in `result.json`. This means the base database was read, not that all recent application messages were necessarily captured. Report the actual message time range; compare against an authorized known message when freshness matters. Storage formats outside these databases, unsynchronized phone messages, and unavailable attachments remain gaps.

## Attachment identity and decoding

For supported ordinary image messages, the local cache basename comes only from the complete protobuf parse at field path `3.4`: `PackedInfoDataImg2.imageInfo.filename`. Accept exactly one such field containing a 32-character hexadecimal basename. Unknown, duplicate, malformed, or truncated layouts are rejected. Never search an arbitrary blob/XML for a 32-character string, and never substitute a different existing file when the specified filename is absent.

Search only the selected contact's `msg/attach/md5(username)/YYYY-MM/Img/` directory for that exact basename. The month is derived from the message timestamp in Asia/Shanghai. Different layouts or files stored elsewhere are reported missing rather than broadly searched. XML `img/@md5`, AES keys, CDN identifiers, and the local filename have different meanings; do not conflate them.

Choose an available `_h.dat`, `.dat`, then `_t.dat` for the same basename. If a tier cannot decode, record its failure before trying the next. This includes Pillow pixel-limit exceptions; retain the pixel protection and try the next tier rather than aborting the whole read. If no tier succeeds, preserve the other records and report `decode_failed`. Report `local_h`, `local_regular`, or `thumbnail`, actual pixel dimensions, and whether a wxgf/HEVC container was converted to PNG. `_h.dat` naming is not independent proof of original upload quality. The tool does not request downloads or click “view original.”

Full decoding and field correspondence establish that the retrieved local image is readable and linked by the tested format; they do not establish a medical conclusion. Visually inspect the retrieved report and independently re-read decision-changing fields as required by the main skill.

## Results and verification

The run writes `result.json` and successfully decoded images in the private output directory. `result.json` includes scoped records, image status/tier/dimensions, source paths, file hashes, database read/recheck statistics, message timestamps, and window observations. It contains sensitive chat material and must not be copied into this skill or an unrelated memory repository.

`ok=true` means the read completed; it does not by itself prove background operation. Check `background_verified` and `minimized_verified` separately. Background verification requires a completed observer, nonzero samples, no missing-window samples, no observed WeChat foreground samples/events, and a background final state. Minimized verification additionally requires initial/final minimization, all samples minimized, and no recorded restore event. Describe these as observed samples/events, not a mathematical guarantee of uninterrupted state between observations.

Run the synthetic regressions and skill validator after changes. Live validation requires authorized task data; do not send a test message or manipulate the user's application to manufacture a result. Keep the old full skill snapshot available for rollback.

## Verification coverage

The 0.2.0 base implementation was exercised locally against Windows Weixin 4.1.15.55 with a minimized, logged-in client. Both text and a locally stored image were retrieved while the observer recorded no foreground or restore event. That run did not establish complete history or universal real-time coverage. Its source WAL files had no current-generation committed frames, so the results came from base databases.

Version 0.2.1 makes the Windows interpreter explicit, bounds observer startup and shutdown, rejects output within the repository, and checks observation failures without treating them as successful background verification. Its regression suite uses synthetic databases, generated images, and disposable observer processes. It does not access a real account. Run `python3 -B -m unittest discover -s scripts -p 'test_*.py' -v` from the repository root. Full end-to-end retrieval on other WeChat versions still needs a scoped local check; a green CI run is not that check.

Observer readiness is limited to 10 seconds. Normal shutdown is limited to 5 seconds; a hung child is terminated and reaped, with a further bounded wait. A missing, malformed, or incomplete completion report leaves background/minimized verification false. Completed reads retain those observation failures in their output rather than claiming that invisibility was verified.

## Source attribution

- Isolated Config.Cipher extraction and image crypto were adapted from [fanyuantaier/wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica/tree/d322c4a9b1124228820bab363e5676016f188fc6), `wechatauto/db.py` and `media.py`, Apache-2.0. Full upstream license is in `LICENSE.wechatauto`; copied code is marked with its modifications. The complete upstream runtime is not imported.
- The independently written minimal protobuf parser uses field-layout evidence from [omni-bot-sdk-oss, packed_info_data_img2.proto](https://github.com/weixin-omni/omni-bot-sdk-oss/blob/7c73235fe5d551a35c396b87a419615847239eec/src/omni_bot_sdk/weixin/parser/util/protocbuf/packed_info_data_img2.proto#L3). No GPL SDK implementation is bundled. This is a community reverse-engineered schema, checked against a local image on 4.1.15.55, not an official Tencent API contract.
- [SQLite WAL format](https://www.sqlite.org/fileformat2.html#walformat) and [SQLCipher API](https://www.zetetic.net/sqlcipher/sqlcipher-api/) define the checksum/raw-key semantics used by the adapter.
