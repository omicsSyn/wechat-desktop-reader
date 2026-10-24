# wechat-desktop-reader

读取 Windows 微信/Weixin 中指定个人联系人的本地聊天和图片，供 Codex、Claude Code 等支持 `SKILL.md` 的助手使用。版本：0.2.1。

提供两种模式：

| 模式 | 输入 | 输出 | 微信窗口状态 |
| --- | --- | --- | --- |
| 本地后台读取 | 已登录进程、明确的账号目录、精确个人联系人、读取数量 | 指定聊天的 `result.json` 与可解码的本地图片 | 可保持最小化；不点击或切换聊天 |
| 桌面采集 | 当前窗口、联系人名称、经过检查的截图 | 截图及带哈希的快照记录 | 窗口需未最小化；采集期间避免操作同一微信窗口 |

本地后台模式目前仅支持个人联系人，不支持群聊。桌面模式按当前窗口采集，群聊尚未进行专项实机验证。

后台模式按需解密数据库页，不导出整库，不保存运行密钥，不发送消息。桌面模式保留截图校验、搜索、点击和滚动，用于本地缓存无法覆盖的材料。脚本没有上传接口；把结果交给模型分析时，数据处理方式取决于所用客户端和模型设置。

## 环境

- Windows x64，已登录的 Weixin 客户端；现有实机验证范围为 **4.1.15.55**。
- WSL Linux 与 Python 3.11 或 3.12，用于主程序和数据库只读适配器。
- Windows x64 Python 3.11 或 3.12，用于两个仅依赖标准库的辅助进程；通过 `--windows-python` 明确指定。
- WSL 侧依赖：APSW、cryptography、Pillow，以及系统 `libzstd`。
- `ffmpeg` 仅用于 wxgf/HEVC 图片转换；Windows PowerShell 用于桌面模式和窗口状态查询。

本地数据库模式不支持直接在原生 Windows Python、macOS 或无 Windows 互操作的 Linux 上运行。不同客户端版本或存储格式需要重新验证。

## 安装

把仓库下载或克隆为 `wechat-desktop-reader` 目录。在 WSL 中进入该目录，创建独立环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Ubuntu/WSL 缺少系统组件时，可安装：

```bash
sudo apt-get install python3-venv libzstd1 ffmpeg
```

将仓库目录放入或链接到客户端的 skills 目录，例如 Codex 的 `~/.codex/skills/wechat-desktop-reader` 或 Claude Code 的 `~/.claude/skills/wechat-desktop-reader`。已有同名 skill 时先保留旧版本，不覆盖。客户端若已共享同一个 skills 目录，只安装一次。执行脚本时使用刚创建的 `.venv/bin/python`。

助手的入口是 [SKILL.md](SKILL.md)，后台读取细节见 [references/local-mode.md](references/local-mode.md)。`review_auditor` 是可选的审核角色名称，不是安装依赖。

## 本地后台读取

先查询当前窗口和进程信息：

```bash
.venv/bin/python scripts/reader.py status
```

从结果中选取目标微信主窗口的 PID、handle 和 version。账号目录须是包含 `db_storage` 与 `msg` 的那一层；`--account` 是准确的已登录账号标识，不是联系人昵称，也不应直接照抄带额外后缀的目录名。`--contact` 必须唯一精确匹配联系人备注或昵称。同名、账号不匹配或缺少数据库密钥时会停止。

下面是 Bash 模板。替换所有尖括号内容后执行；它不是可直接照抄的真实账号配置：

```bash
.venv/bin/python scripts/local_reader.py \
  --account-dir '/mnt/d/<account-directory>' \
  --account '<account-id>' \
  --contact '<contact-name>' \
  --pid <current-pid> \
  --hwnd <main-window-handle> \
  --windows-python '/mnt/c/<windows-python-directory>/python.exe' \
  --limit 30 \
  --images 8 \
  --require-minimized \
  --output '/mnt/d/<private-output>/<new-run-directory>'
```

输出目录必须尚不存在，并且位于账号目录和 skill 仓库之外。`--limit` 为最近 1–100 条消息；`--images` 独立选择最近 1–20 条普通图片消息，因此图片可能早于这批文本消息。当前没有日期筛选，也不支持把图片数量设为零。

带 `--require-minimized` 时，请先手动最小化微信。读取期间可以使用其他软件；脚本不会恢复微信或切换窗口。不带该参数时也可读取被其他窗口遮住的微信，最终按实际观察结果报告后台状态。

不要直接运行 `scripts/_windows_keys.py`：它的标准输出是父进程内部使用的密钥通道。正常入口会私下捕获该输出，不把密钥放到终端、文件或日志。

## 查看结果

`result.json` 保存指定聊天的消息、时间、来源分片、图片状态、图片尺寸及窗口观察结果；可解码图片保存为 `image_01.jpg` 等文件。输出包含聊天内容及来源路径，应保留在自己的任务目录中，不提交到仓库或公开 Issue。

| 字段或状态 | 含义 |
| --- | --- |
| 终端摘要 `ok=true` | 本次读取完成，不等同于后台状态已验证 |
| `background_verified` | 观察期间未记录到微信前台状态或激活事件，且观察完整 |
| `minimized_verified` | 在后台验证通过基础上，起止及所有样本均最小化，未记录到恢复事件 |
| `local_h` / `local_regular` / `thumbnail` | 本地高层级缓存、普通缓存或缩略图；均不自动等于原始上传图片 |
| `missing_local_file` / `decode_failed` | 本地文件缺失或解码失败，不能算已读取 |
| `unsupported_attachment_metadata` | 图片关联结构不支持，不按相似文件名猜测图片 |
| 普通文本 `text=null` | 文本未成功解码，需要明确保留此缺口 |

窗口结果来自采样和事件记录，不是对每个瞬间的绝对保证。数据库结果来自受校验的读取，不是跨分片原子快照。新消息尚未同步、转发合并消息、语音、视频、其他存储布局和缺失附件不在完整支持范围内。

## 桌面模式

```bash
.venv/bin/python scripts/reader.py capture --window 微信 --chat '<contact-name>'
```

先检查返回的图片及聊天标题，再按 [SKILL.md](SKILL.md) 使用对应快照进行搜索、点击或滚动。桌面模式会改变可见聊天位置，采集时避免同时操作微信。截图和快照默认保存在 Windows 临时目录中的独立会话内；它们不是原始附件。

检查报告等重要材料需要回看原图。左右侧、日期、数值、单位、否定词和“考虑/疑似”等措辞应逐项核对；不能把首次 OCR 或摘要当作复核。

## 测试与版本说明

```bash
.venv/bin/python -B -m unittest discover -s scripts -p 'test_*.py' -v
```

测试只使用合成数据库、合成图片和临时子进程，不读取真实账号。GitHub Actions 运行 Python 3.11/3.12 的相同测试；首次推送后的远端执行状态以仓库 Actions 页面为准。

0.2.1 基于已进行本地实机验证的 0.2.0，保留两种读取模式，并增加显式 Windows Python 路径、观察进程启动/退出超时、失败状态校验、仓库内输出拦截及超大像素图片失败后的分级降级处理。此次发布整理没有再次读取真实聊天；合成测试不代表所有微信版本均已验证。更多实现边界见 [references/implementation.md](references/implementation.md)。

## 许可证与来源

项目采用 [Apache-2.0](LICENSE)。改编代码的固定来源版本、修改说明和上游许可证见 [THIRD_PARTY.md](THIRD_PARTY.md) 与 [LICENSE.wechatauto](LICENSE.wechatauto)。仓库只包含源代码、说明和合成测试，不包含微信客户端、真实聊天、检查报告或运行密钥。
