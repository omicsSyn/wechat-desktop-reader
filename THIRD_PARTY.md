# Third-party source attribution

## Adapted implementation

Selected process-memory extraction and image-decryption logic was adapted from `fanyuantaier/wechatauto-replica` at commit `d322c4a9b1124228820bab363e5676016f188fc6`:

- [`wechatauto/db.py`](https://github.com/fanyuantaier/wechatauto-replica/blob/d322c4a9b1124228820bab363e5676016f188fc6/wechatauto/db.py): `_windows_keys.py` retains the relevant Windows structures, Config.Cipher discovery, account-configuration lookup, and first-page authentication logic. The adapter uses an explicit PID/account/database list, bounded scanning, private captured stdout, and no key cache or package runtime.
- [`wechatauto/media.py`](https://github.com/fanyuantaier/wechatauto-replica/blob/d322c4a9b1124228820bab363e5676016f188fc6/wechatauto/media.py): `local_reader.py` adapts image decoding into a scoped file-only reader with structural attachment lookup and full decode validation. Download, network, UI, and arbitrary-hash fallback routines are not included.

The source repository declares Apache-2.0 at that revision. Its complete [license](https://github.com/fanyuantaier/wechatauto-replica/blob/d322c4a9b1124228820bab363e5676016f188fc6/LICENSE) is retained as [LICENSE.wechatauto](LICENSE.wechatauto). Modified source files carry attribution and modification notices. No upstream NOTICE file was present in the inspected pinned tree. The complete upstream package is neither bundled nor imported.

## Format references

The independently written protobuf wire parser uses the field-layout reference [`packed_info_data_img2.proto`](https://github.com/weixin-omni/omni-bot-sdk-oss/blob/7c73235fe5d551a35c396b87a419615847239eec/src/omni_bot_sdk/weixin/parser/util/protocbuf/packed_info_data_img2.proto), pinned to commit `7c73235fe5d551a35c396b87a419615847239eec` of `weixin-omni/omni-bot-sdk-oss`. The reference identifies `imageInfo.filename` at field path `3.4`. No SDK implementation, generated protobuf module, or `.proto` file is bundled.

Database-page and WAL semantics are cross-checked against the [SQLite file format](https://www.sqlite.org/fileformat2.html#walformat) and [SQLCipher API](https://www.zetetic.net/sqlcipher/sqlcipher-api/). The page-based VFS and its synthetic test fixtures are implemented within this project.

## Runtime dependencies

APSW, cryptography, and Pillow are installed separately through `requirements.txt`. The operating system supplies libzstd, optional ffmpeg, and Windows APIs. Their distributions are not vendored in this repository. Windows helpers use the Python standard library only.
