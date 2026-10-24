# Adapted from fanyuantaier/wechatauto-replica, wechatauto/db.py (Apache-2.0).
# Source commit: d322c4a9b1124228820bab363e5676016f188fc6.
# Changes: explicit account/PID/database scope; raw-key HMAC validation; bounded
# scanning; no key files, UI, network, or package import. Private stdout contains
# secret IPC data: only launch through local_reader.py, which captures it.
import ctypes, struct, json, sys, os
from ctypes import wintypes
from typing import Optional, Tuple
_k32 = ctypes.WinDLL('kernel32', use_last_error=True)
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.ReadProcessMemory.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
CFG_LANDMARK = b'global_config'
CFG_PTR_BACK = 312
CFG_OFFSET = 104
CFG_DWORD_OFF = 64
CFG_WXID_OFF = 72

class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('th32ModuleID', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD), ('GlblcntUsage', wintypes.DWORD), ('ProccntUsage', wintypes.DWORD), ('modBaseAddr', ctypes.c_void_p), ('modBaseSize', wintypes.DWORD), ('hModule', wintypes.HMODULE), ('szModule', ctypes.c_wchar * 256), ('szExePath', ctypes.c_wchar * 260)]

def _find_weixin_module(pid: int) -> Optional[Tuple[int, int, str]]:
    """返回 (模块基址, 模块大小, 路径); weixin.dll 为微信 4.x 主模块"""
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.Module32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MODULEENTRY32W)]
    k32.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MODULEENTRY32W)]
    snap = k32.CreateToolhelp32Snapshot(8 | 16, pid)
    if not snap or snap == -1 or snap == (1 << 64) - 1:
        return None
    try:
        me = _MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(me)
        ok = k32.Module32FirstW(snap, ctypes.byref(me))
        while ok:
            if me.szModule and me.szModule.lower() == 'weixin.dll':
                return (me.modBaseAddr or 0, me.modBaseSize, me.szExePath)
            me.dwSize = ctypes.sizeof(me)
            ok = k32.Module32NextW(snap, ctypes.byref(me))
    finally:
        k32.CloseHandle(snap)
    return None

def _read_remote_string(h, read, addr: int) -> str:
    """跨进程读 MSVC x64 std::string(SSO: size<=15 内联, 否则堆指针)"""
    sz_buf = read(addr + 16, 8)
    if not sz_buf:
        return ''
    size = struct.unpack_from('<Q', sz_buf)[0]
    if size <= 0 or size > 256:
        return ''
    if size <= 15:
        data = read(addr, size)
    else:
        p_buf = read(addr, 8)
        if not p_buf:
            return ''
        data = read(struct.unpack_from('<Q', p_buf)[0], size)
    if not data:
        return ''
    return data[:size].decode('utf-8', 'replace')

def read_account_config(pid: int) -> Optional[Tuple[int, str]]:
    """Read the account identity and image config only; cfg database master keys are not trusted."""
    base, mod_size, dll_path = _find_weixin_module(pid) or (0, 0, '')
    if not base or not dll_path or mod_size <= 0 or (mod_size >= 1073741824):
        return None
    h = _k32.OpenProcess(16 | 1024, False, pid)
    if not h:
        return None
    try:

        def read(addr: int, n: int):
            buf = ctypes.create_string_buffer(n)
            br = ctypes.c_size_t(0)
            if _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
                return buf.raw[:br.value]
            return None
        image = read(base, mod_size)
        if not image or len(image) != mod_size:
            return None
        pos = -1
        for i in range(len(image) - 16, 0, -8):
            if struct.unpack_from('<I', image, i)[0] == len(CFG_LANDMARK):
                cap = struct.unpack_from('<I', image, i + 8)[0]
                if cap and cap | 15 == 15 and (i - 16 >= 0):
                    if image[i - 16:i - 16 + len(CFG_LANDMARK)] == CFG_LANDMARK:
                        pos = i
                        break
        if pos < 0:
            return None
        v18_buf = read(base + pos - CFG_PTR_BACK, 8)
        if not v18_buf:
            return None
        v18 = struct.unpack_from('<Q', v18_buf)[0]
        cfg_buf = read(v18 + CFG_OFFSET, 8)
        if not cfg_buf:
            return None
        cfg = struct.unpack_from('<Q', cfg_buf)[0]
        if not 65536 <= cfg < 140737488355328:
            return None
        dw_buf = read(cfg + CFG_DWORD_OFF, 4)
        cfg_dword = struct.unpack_from('<I', dw_buf)[0] if dw_buf else 0
        wxid = _read_remote_string(h, read, cfg + CFG_WXID_OFF)
        return (cfg_dword, wxid)
    finally:
        _k32.CloseHandle(h)
import re, hashlib, hmac as hmac_mod, time
from pathlib import Path
from typing import List
PAGE_SZ = 4096
RESERVE_SZ = 80
CONFIG_CIPHER_NAME = b'com.Tencent.WCDB.Config.Cipher'
CONFIG_XOR_MASK = bytes.fromhex('d2c7442458020000004889442450488b450048844c2448488944254048584c24')
HEX_LITERAL_RE = re.compile(b"[xX]'([0-9a-fA-F]{64,192})'")

class _MBI(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p), ('AllocationProtect', wintypes.DWORD), ('__alignment1', wintypes.DWORD), ('RegionSize', ctypes.c_size_t), ('State', wintypes.DWORD), ('Protect', wintypes.DWORD), ('Type', wintypes.DWORD), ('__alignment2', wintypes.DWORD)]

def _pbkdf2(passwd: bytes, salt: bytes, iters: int) -> bytes:
    return hashlib.pbkdf2_hmac('sha512', passwd, salt, iters, dklen=32)

def _verify_enc_key(enc_key: bytes, page1: bytes, salt: Optional[bytes]=None) -> bool:
    """验证 enc_key 是否为 page1 的 SQLCipher 4 密钥。

    - enc_key 为 32 字节裸 key：默认用文件头前 16 字节作 salt（标准形式）；
    - enc_key 为 48 字节 key+salt：显式 salt 优先于文件头（明文头模式）；
    - 也允许单独传 salt 参数覆盖（供提取逻辑按 96hex 拆分尝试）。
    """
    if len(page1) < PAGE_SZ:
        return False
    if len(enc_key) == 48 and salt is None:
        enc_key, salt = (enc_key[:32], enc_key[32:])
    if salt is None:
        salt = page1[:16]
    elif len(salt) != 16:
        return False
    mac_salt = bytes((b ^ 58 for b in salt))
    mac_key = _pbkdf2(enc_key, mac_salt, 2)
    hmac_data = page1[16:PAGE_SZ - RESERVE_SZ + 16]
    stored_hmac = page1[PAGE_SZ - 64:PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack('<I', 1))
    return hm.digest() == stored_hmac
_k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_MBI), ctypes.c_size_t]
_k32.VirtualQueryEx.restype = ctypes.c_size_t

class Scanner:

    def __init__(self):
        self.deadline = time.monotonic() + 90

    def _collect_key_candidates(self, pid: int, seen: set) -> set:
        """收集候选密钥材料（**不做账号校验**）：{(cand, salt_or_None), ...}
    
            与 `_extract_keys_pid` 的区别：这里只负责“找出可能是密钥的 32 字节
            （或 32+16 带显式 salt）”，校验交给调用方对**每个账号目录**分别做。
            原因：内存里的密钥属于**当前登录账号**，只有对正确的账号目录才能通过
            页1 HMAC；这样账号选择由密码学校验决定，不再依赖“最近修改 .db”启发式
            （微信每次更新会重写 .db，mtime 全变 → 启发式会选错账号 → 0 密钥）。
            """
        h = _k32.OpenProcess(16 | 1024, False, pid)
        if not h:
            return set()
        out: set = set()
        try:

            def read(addr: int, n: int):
                buf = ctypes.create_string_buffer(n)
                br = ctypes.c_size_t(0)
                if _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
                    return buf.raw[:br.value]
                return None
            needles = self._find_bytes(h, read, CONFIG_CIPHER_NAME)
            pairs = [struct.pack('<Q', addr) + struct.pack('<Q', len(CONFIG_CIPHER_NAME)) for addr in needles]
            for pair in pairs:
                for qaddr in self._find_bytes(h, read, pair):
                    node = read(qaddr - 16, 80)
                    if not node or len(node) < 64:
                        continue
                    if struct.unpack_from('<Q', node, 16)[0] not in needles:
                        continue
                    if struct.unpack_from('<Q', node, 24)[0] != len(CONFIG_CIPHER_NAME):
                        continue
                    config_ptr = struct.unpack_from('<Q', node, 40)[0]
                    if not 65536 <= config_ptr < 140737488355328:
                        continue
                    obj = read(config_ptr + 136, 40)
                    if not obj or len(obj) < 24:
                        continue
                    data_ptr = struct.unpack_from('<Q', obj, 8)[0]
                    data_len = struct.unpack_from('<Q', obj, 16)[0]
                    if not (0 < data_len <= 1024 and 65536 <= data_ptr < 140737488355328):
                        continue
                    blob = read(data_ptr, int(data_len))
                    if not blob or len(blob) != data_len:
                        continue
                    decoded = bytes((v ^ CONFIG_XOR_MASK[i % len(CONFIG_XOR_MASK)] for i, v in enumerate(blob)))
                    for m in HEX_LITERAL_RE.finditer(decoded):
                        run = m.group(1).decode().lower()
                        starts = [0]
                        if len(run) > 96:
                            starts += list(range(0, len(run) - 63, 32))
                            starts.append(len(run) - 64)
                        for s in dict.fromkeys(starts):
                            if s + 64 > len(run):
                                continue
                            cand = bytes.fromhex(run[s:s + 64])
                            if not self._probable_key(cand):
                                continue
                            out.add((cand, None))
                            if s + 96 <= len(run):
                                out.add((cand, bytes.fromhex(run[s + 64:s + 96])))
        finally:
            _k32.CloseHandle(h)
        return out

    @staticmethod
    def _probable_key(b: bytes) -> bool:
        return len(b) == 32 and len(set(b)) >= 15 and (b not in {b'\x00' * 32, b'\xff' * 32})

    def _find_bytes(self, h, read, needle: bytes) -> List[int]:
        hits = []
        addr = 0
        while True:
            if time.monotonic() > self.deadline:
                raise TimeoutError('Read-only key scan time limit')
            mbi = _MBI()
            r = _k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if r == 0:
                break
            if mbi.State == 4096 and mbi.Protect & 255 & 230 and (not mbi.Protect & 256) and (0 < mbi.RegionSize < 268435456):
                buf = read(mbi.BaseAddress or 0, mbi.RegionSize)
                if buf:
                    base = mbi.BaseAddress or 0
                    pos = 0
                    while True:
                        pos = buf.find(needle, pos)
                        if pos < 0:
                            break
                        hits.append(base + pos)
                        pos += 1
            addr = (mbi.BaseAddress or 0) + mbi.RegionSize
        return hits
if __name__ == '__main__':
    try:
        pid = int(sys.argv[1])
        expected = sys.argv[2]
        root = Path(sys.argv[3])
        cfg = read_account_config(pid)
        if not cfg or cfg[1] != expected:
            raise RuntimeError('Account identity mismatch')
        request = json.loads(sys.stdin.readline())
        allowed = request['databases']
        if not allowed or len(allowed) > 65 or len(set(allowed)) != len(allowed):
            raise ValueError('Invalid database scope')
        if root.name != 'db_storage' or not root.parent.name.startswith(expected + '_'):
            raise ValueError('Account path mismatch')
        if any((not re.fullmatch('contact/contact\\.db|message/message_\\d+\\.db', rel) for rel in allowed)):
            raise ValueError('Database is outside supported scope')
        pages = {}
        for rel in allowed:
            with (root / rel).open('rb') as f:
                pages[rel] = f.read(PAGE_SZ)
        candidates = Scanner()._collect_key_candidates(pid, set())
        valid = {}
        for cand, salt in candidates:
            for rel, page in pages.items():
                if rel in valid:
                    continue
                if _verify_enc_key(cand, page):
                    valid[rel] = cand.hex()
                elif salt and _verify_enc_key(cand, page, salt):
                    valid[rel] = (cand + salt).hex()
        print(json.dumps({'keys': valid, 'image_config': cfg[0], 'account': cfg[1], 'candidate_count': len(candidates)}))
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__}))
        sys.exit(1)
