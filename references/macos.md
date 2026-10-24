# macOS 实验性本地读取

版本 0.3.0-alpha.1。此入口在原生 macOS Python 中运行，复用只读 SQLCipher 页面适配器，读取明确指定账号和个人联系人的本地消息及可解码图片。不需要 WSL、Windows Python、PowerShell 或桌面采集。

当前需要使用者已有的有效逐库原始密钥。程序不包含 Mac 进程密钥提取，不启动、重签或调试微信，不修改系统权限。没有有效密钥时，只能运行环境和目录检查，不能读取聊天。不要让用户把密钥粘贴到聊天、命令行参数或公开 Issue；不要用 `cat` 等工具把密钥文件内容返回给模型。

## 验证范围

- 支持目标：macOS 15 Apple Silicon 和 Intel，原生 Python 3.11/3.12。GitHub Actions 配置这两个系统架构的合成测试，实际通过状态见仓库 Actions。
- 尚无真实 Mac 微信验证，也没有得到验证的 Mac 微信版本号。不能把 Windows 4.1.15.55 的实测结论移到 Mac。
- 仅支持相同字段布局的 `contact/contact.db`、`message/message_N.db`，SQLCipher 4 的 4096 字节页、80 字节保留区、32 字节 raw key 和受支持 WAL。Mac 3.x 或其他 schema 不适用。
- 图片关联仅支持消息 `packed_info_data` 字段路径 `3.4` 中唯一的 32 位十六进制文件名。需要 `MessageResourceInfo` / `MessageResourceDetail` 资源表关联的 Mac 布局不支持，返回 `unsupported_attachment_metadata`，不扫描任意 hash 或用相似图片替代。
- 支持明文 JPEG/PNG/GIF/WebP、旧 XOR 图片和已有解码器支持的 V2 DAT。V2 还需要独立的 `image_config`；数据库密钥不是图片密钥。V1 DAT、其他容器和未下载附件不在支持范围。WXGF 转换仅尝试 HEVC 第一帧，不保证动画、透明度或全部编码变体。

## 安装

进入仓库目录后创建独立环境。以下命令使用已安装的 Homebrew；安装依赖前遵守当前任务的授权范围。

```bash
brew install python@3.12 zstd
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

只有需要 WXGF/HEVC 转换时才需要 `brew install ffmpeg`。`libzstd` 优先使用系统动态库查找，并兼容 `/opt/homebrew/lib/libzstd.dylib` 和 `/usr/local/lib/libzstd.dylib`。找不到该库时，普通 UTF-8 文本仍可读取，压缩文本保留为 `text=null`，不能报告成空消息或已读正文。

账号目录必须由使用者明确选择，且为包含 `db_storage` 与 `msg` 的那一层。Mac 4.x 的一个社区报告布局是：

```text
/Users/<user>/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<account-directory>
```

这是布局线索，不是自动选择规则；不同安装和版本可能不同。程序不遍历全部账号或自动选择第一个账号。若系统拒绝访问，由使用者核对该目录和终端的系统权限；不要用提权或更改微信客户端来绕过失败。

```bash
.venv/bin/python scripts/mac_reader.py status \
  --account-dir '/Users/<user>/<actual-account-directory>'
```

`status` 只检查操作系统、依赖和指定目录中的支持数据库文件名，不读取密钥或聊天。它列出的数据库是随后需要的完整密钥集合。`ok=true` 不代表密钥、数据库 schema、登录状态或图片兼容性已经验证；`zstd_found` 和 `ffmpeg_found` 单独报告。

## 已有密钥文件

使用者在仓库和账号目录之外提供一个本人所有、权限为 `0600` 或 `0400` 的普通 JSON 文件。程序只读取，不生成或修改它。拒绝文件符号链接、重复 JSON 字段和大于 1 MiB 的文件。权限检查采用 POSIX 所有者及模式位，不是操作系统 ACL 或磁盘加密审计。

格式如下，所有尖括号字段都须替换；此示例不能直接运行：

```json
{
  "version": 1,
  "account_dir": "/Users/<user>/<actual-account-directory>",
  "account": "<exact-account-id>",
  "keys": {
    "contact/contact.db": "<64 hexadecimal characters for this database>",
    "message/message_0.db": "<64 hexadecimal characters for this database>",
    "message/message_1.db": "<64 hexadecimal characters for this database>"
  }
}
```

`keys` 必须恰好包括 `status` 找到的 contact 库和全部 `message_N.db`，不接受其他库或缺失分片；只有一个消息分片时不要保留示例的第二个分片。这里的 raw key 是已经派生好的 32 字节值，不是登录密码、口令、数据库盐，也不是 SQLCipher 带盐的 48 字节格式。每库读页时继续做 HMAC 校验，不能通过删除校验来接受错误密钥。

若已有适用于该账号的图片配置数值，可额外增加顶层 `"image_config": <unsigned-integer>`。当前 V2 解码复用 `MD5(str(image_config) + account)` 的前 16 个十六进制字符作为 AES 字节，并用 `image_config & 255` 处理尾部；此格式尚未在本项目的 Mac 客户端实测。不要从其他账号复制配置，也不要把 SQLCipher key 填到这个字段。没有它时仍可读文字、明文图片和支持的旧 XOR 图片。

`account_dir` 的规范绝对路径和 `account` 必须与本次参数一致。该绑定防止误用另一份配置，但账号身份仍是使用者提供的声明，不构成对登录进程身份的独立核验。`account` 用于发送者分类和图片派生，须填写真实内部账号 ID，不能填昵称或凭目录后缀猜测。

## 读取指定个人联系人

以下 Bash 模板须替换占位符，输出目录必须尚不存在，且位于账号目录和仓库之外：

```bash
.venv/bin/python scripts/mac_reader.py read \
  --account-dir '/Users/<user>/<actual-account-directory>' \
  --account '<exact-account-id>' \
  --contact '<exact-contact-nickname-or-remark>' \
  --key-file '/Users/<user>/<private-directory>/keys.json' \
  --timezone 'Asia/Shanghai' \
  --limit 30 \
  --images 8 \
  --output '/Users/<user>/<private-output>/<new-run-directory>'
```

`--timezone` 必须明确指定实际附件月份目录所用的 IANA 时区；`Asia/Shanghai` 仅为示例。月份和显示日期按该时区计算，原始 Unix 时间戳保留。跨时区迁移或旧附件采用不同月份规则时，可能返回 `missing_local_file`，不能扩大搜索范围猜配图片。

`--limit` 范围 1–100；`--images` 范围 0–20，设为 0 时只读取消息记录，不查图片文件。图片独立取最近 N 条普通图片消息，可能早于最近的 N 条文本/消息记录。同名、找不到精确匹配、群聊、缺少分片密钥或不兼容数据库会停止。

程序通过文件读取工作，不启动微信、调用 AppleScript、请求屏幕录制或辅助功能权限，也不恢复最小化窗口。可以继续操作其他软件。文件正在变化时会执行已有的来源复查，并最多重试一次；这仍不是跨数据库原子快照，也不保证最新尚未同步的消息已在磁盘。

## 结果及第一次实机核对

输出目录以 `0700` 创建，JSON 和已解码图片通过 `umask 077` 创建。结果包含私密聊天，留在用户任务目录。账号原文件和密钥文件不修改。失败后的部分输出不表示读取完成，不自动删除；重试须指定新的输出目录。

Mac 结果保留 `experimental=true`、`live_wechat_tested=false`、`ui_automation_used=false`。`background_verified` 和 `minimized_verified` 固定为 `null`，`background_observation.supported=false`，因为本版未实现 Mac 窗口观察器。不要把文件读取不使用 UI 写成已观察到整个过程始终最小化。

私密 `result.json` 同时保存 `account` 和规范 `account_dir`，即使 `--images 0` 也能追溯选定的账号和来源；这两个字段不进入终端摘要。找不到联系人、重名或群聊分别返回 `contact_not_found`、`contact_ambiguous`、`individual_contact_required`，据此核对准确备注/昵称或改选个人聊天，不要为这些业务错误重装依赖或重新获取密钥。

图片可能返回 `decoded`、`missing_local_file`、`unsupported_attachment_metadata`、`missing_image_key` 或 `decode_failed`；失败层级记录在 `failed_tiers`。任何图片级失败都应保留，成功解码也不能单凭文件名认定为原始上传画质。终端只返回计数、状态和输出位置，不返回正文或密钥；正文在私密 `result.json` 中。

有可用 Mac 后，在用户授权的单个个人聊天中核对一条已知文字和一张已知图片的联系人、发送者、时间、内容、尺寸与文件哈希；记录实际芯片、macOS 和微信版本。另行观察微信最小化和其他应用焦点，确认来源文件未被本工具修改。该步骤未完成前，保持实验性标记；合成测试通过不能代替它。

## 设计依据

- [macOS WeChat 4.x 社区工具的固定版本说明](https://gist.github.com/acmerfight/0a01249ef72970d07a2603dbb629e80f/c98dbb10cf23c30e83d5d744905101b0880887a0) 提供存储布局和数据库/图片格式的线索，其作者报告的实机结果不算本项目验证。这里只参考布局与限制，未复制或调用该工具的实现。
- [SQLCipher raw key 与配置文档](https://www.zetetic.net/sqlcipher/sqlcipher-api/)；[SQLite WAL 格式](https://www.sqlite.org/fileformat2.html#walformat)。
- [GitHub 托管 runner 列表](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) 用于选择标准 Apple Silicon 和 Intel 合成测试环境。
