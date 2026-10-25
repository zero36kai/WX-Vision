# -*- coding: utf-8 -*-
"""隐私扫描:发布到 GitHub 前的最后一道闸(README §十「发布前须知」)。

exit 0 = 通过(末行打印 PRIVACY_PASS);任一断言失败 exit 1(PRIVACY_FAIL)。

扫描范围:仓库全部「文本文件」(UTF-8 可解码且不含 NUL 即视为文本)递归扫描;
凡被 .gitignore 覆盖的路径(config.json / cloudflared/ / tests/_debug/ / logs/
docs/research/screenshots/ / .zcode/ 等)与 .git 一律跳过——它们本就不入库
(例外:*.pyc 编译产物全树必扫,见断言 2b——整目录 zip 打包/上传会连被忽略
路径一起带出)。

断言:
 1. 无词边界的真实联系人缩写名(两个字母;词边界按 ASCII 字母数字
    判定,比 re 的 \\b 更严——可捕获两字母缩写与中文紧邻的形态,如
    「仅限<两字母>」「目标<两字母>。」;本注释亦不含该缩写明文);
 2. 无真实聊天列表联系人名(两个中文昵称 + 一个英文昵称,源码中一律以
    chr 码点运行期构造,本文件自身不含明文;文档中已脱敏为「示例联系人」);
 2b. 编译产物(*.pyc)全树扫描(含被 .gitignore 忽略的路径):pyc 内嵌
    常量不得含上述敏感串——字面量拼接与 \\uXXXX 转义都会被 CPython 编译期
    常量折叠还原成明文写进 .pyc,源码干净 ≠ 编译产物干净;且 __pycache__/
    虽不入库,整目录 zip 打包/上传仍会带出(第二轮审查实测残留即由此漏检);
 3. .gitignore 覆盖口令/凭据/截图/日志等六类路径:config.json、cloudflared/,
    tests/_debug/,logs/,data/,docs/research/screenshots/,.zcode/。

注意:本文件自身也参与扫描——因此所有敏感串均以 chr 码点运行期构造
(不可折叠进编译产物),若直接把明文写进本文件,扫描会(正确地)报失败。

红线:纯本地文本扫描;不触碰微信窗口、不发送任何消息、不解析 config.json 内容。
"""
from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ---------------------------------------------------------------- 敏感串
# 全部以 chr 码点在运行期构造:本文件源码不含任何敏感串明文(否则自己都过
# 不了自己);且不可改回字面量拼接或 "\uXXXX" 转义——CPython 编译期常量折叠
# 会把它们还原成明文常量写进 .pyc(第二轮审查实测:修复后的源码一经重新
# 编译,__pycache__ 里立刻复活明文)。运行期表达式不会被折叠,断言 2b 把关。
_REAL_CONTACT_INITIALS = "".join(map(chr, (0x41, 0x4B)))                       # 真实联系人缩写(词边界匹配)
_REAL_CN_NICK_1 = "".join(map(chr, (0x840D, 0x7EC8, 0x4FA0, 0x5F71)))          # 真实昵称一(4 字)
_REAL_EN_NICK = "".join(map(chr, (0x5A, 0x65, 0x72, 0x6F)))                    # 真实英文昵称
_REAL_CN_NICK_2 = "".join(map(chr, (0x672A, 0x547D, 0x540D, 0x8D26, 0x53F7)))  # 真实昵称二(5 字)

# 词边界:两侧不得是 ASCII 字母/数字(下划线与 CJK 视为边界,从严)。
_INITIALS_PATTERN = re.compile(
    "(?<![A-Za-z0-9])" + _REAL_CONTACT_INITIALS + "(?![A-Za-z0-9])")

# .gitignore 必须覆盖的路径(README §十一 表格一致;data/ 为信箱聊天数据)
_MUST_IGNORE = (
    "config.json",
    "cloudflared/",
    "tests/_debug/",
    "logs/",
    "data/",
    "docs/research/screenshots/",
    ".zcode/",
)

_MAX_BYTES = 10 * 1024 * 1024   # 超过 10MB 视为非文本,跳过(仓库无此类文件)


# ---------------------------------------------------------------- .gitignore
def _load_gitignore() -> list[tuple[str, bool]]:
    """解析 .gitignore → [(pattern, dir_only)];空文件/缺失返回空表。"""
    pats: list[tuple[str, bool]] = []
    gi = ROOT / ".gitignore"
    if not gi.exists():
        return pats
    for line in gi.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        line = line.lstrip("/")          # 本仓库模式均相对根目录
        pats.append((line, dir_only))
    return pats


GITIGNORE_PATS = _load_gitignore()


def _is_ignored(rel_posix: str, is_dir: bool) -> bool:
    """本仓库 .gitignore 的最小匹配实现(名字模式/目录模式/带路径模式/*.通配)。

    rel_posix:相对仓库根、'/' 分隔;is_dir:该路径是否目录。
    语义覆盖本仓库全部写法;若 .gitignore 引入新语法,应同步增强本函数。"""
    parts = rel_posix.split("/")
    for pattern, dir_only in GITIGNORE_PATS:
        if "/" not in pattern:
            # 名字模式:任一路径分量匹配即忽略(目录模式只匹配目录分量)
            for i, comp in enumerate(parts):
                comp_is_dir = is_dir or i < len(parts) - 1
                if dir_only and not comp_is_dir:
                    continue
                if fnmatch.fnmatch(comp, pattern):
                    return True
        else:
            # 带路径模式:前缀逐段匹配;目录模式下其内文件同样忽略
            pat_parts = pattern.split("/")
            if len(parts) >= len(pat_parts) and all(
                fnmatch.fnmatch(parts[i], pat_parts[i])
                for i in range(len(pat_parts))
            ):
                if is_dir or len(parts) > len(pat_parts) or not dir_only:
                    return True
    return False


# ---------------------------------------------------------------- 扫描
def _iter_text_files() -> tuple[list[tuple[Path, str]], list[str]]:
    """返回 ([(相对路径, 文本)], 跳过说明)。忽略 .git 与 .gitignore 覆盖路径。"""
    files: list[tuple[Path, str]] = []
    skipped: list[str] = []
    for dirpath, dirnames, filenames in os_walk_pruned():
        for fn in filenames:
            p = dirpath / fn
            rel = p.relative_to(ROOT).as_posix()
            if _is_ignored(rel, is_dir=False):
                skipped.append(rel)
                continue
            try:
                if p.stat().st_size > _MAX_BYTES:
                    skipped.append(rel + "(>10MB)")
                    continue
                data = p.read_bytes()
                if b"\x00" in data:
                    skipped.append(rel + "(二进制)")
                    continue
                text = data.decode("utf-8")
            except (UnicodeDecodeError, OSError) as e:
                skipped.append(f"{rel}(不可解码/读取: {type(e).__name__})")
                continue
            files.append((p, text))
    return files, skipped


def os_walk_pruned():
    """os.walk 顶层版:剪掉被忽略的目录与 .git(保持确定顺序)。"""
    stack = [ROOT]
    while stack:
        d = stack.pop()
        rel_d = d.relative_to(ROOT).as_posix() if d != ROOT else ""
        try:
            entries = sorted(d.iterdir(), key=lambda x: x.name)
        except OSError:
            continue
        dirs, files = [], []
        for e in entries:
            rel = f"{rel_d}/{e.name}" if rel_d else e.name
            if e.is_dir():
                if e.name == ".git" or _is_ignored(rel, is_dir=True):
                    continue
                dirs.append(e)
            else:
                files.append(e)
        yield d, dirs, files
        stack.extend(reversed(dirs))


# ---------------------------------------------------------------- 编译产物
def _scan_compiled() -> tuple[list[str], int]:
    """断言 2b 用:全树扫描 *.pyc 字节(不经 .gitignore 过滤,忽略路径同样查)。

    字面量拼接与 \\uXXXX 转义会被 CPython 编译期常量折叠还原成明文常量写进
    .pyc;__pycache__/ 虽被 .gitignore 覆盖、不入库,但整目录 zip 打包/上传
    仍会带出(第二轮审查实测残留)。pyc 字符串常量为 UTF-8 编码,忽略无效
    字节解码后按断言 1/2 同样匹配。"""
    hits: list[str] = []
    pycs = sorted(ROOT.rglob("*.pyc"))
    for p in pycs:
        rel = p.relative_to(ROOT).as_posix()
        try:
            text = p.read_bytes().decode("utf-8", "ignore")
        except OSError as e:
            hits.append(f"{rel}(不可读: {type(e).__name__})")
            continue
        if _INITIALS_PATTERN.search(text):
            hits.append(f"{rel}: 缩写")
        for nick in (_REAL_CN_NICK_1, _REAL_EN_NICK, _REAL_CN_NICK_2):
            if nick in text:
                hits.append(f"{rel}: 昵称明文")
    return hits, len(pycs)


def main() -> int:
    failures: list[str] = []

    print(f"加载 .gitignore:{len(GITIGNORE_PATS)} 条模式")
    if not GITIGNORE_PATS:
        failures.append(".gitignore 不存在或为空(隐私闸门依赖它排除私有文件)")

    files, skipped = _iter_text_files()
    print(f"扫描文本文件 {len(files)} 个;跳过(忽略/二进制/不可读){len(skipped)} 项")

    # ---- 断言 1/2:敏感串不得出现在任何入库文本中 ----
    hits_initials: list[str] = []
    hits_nicks: list[str] = []
    for p, text in files:
        rel = p.relative_to(ROOT).as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            if _INITIALS_PATTERN.search(line):
                hits_initials.append(f"{rel}:{i}: {line.strip()[:80]}")
            for nick in (_REAL_CN_NICK_1, _REAL_EN_NICK, _REAL_CN_NICK_2):
                if nick in line:
                    hits_nicks.append(f"{rel}:{i}: {line.strip()[:80]}")

    ok1 = not hits_initials
    print(f"\n[1] 无词边界真实联系人缩写({_REAL_CONTACT_INITIALS}):"
          f"{'PASS' if ok1 else 'FAIL'}(命中 {len(hits_initials)} 处)")
    for h in hits_initials[:20]:
        print(f"      {h}")

    ok2 = not hits_nicks
    print(f"[2] 无真实聊天列表联系人名(2 中文昵称 + 1 英文昵称,已脱敏):"
          f"{'PASS' if ok2 else 'FAIL'}(命中 {len(hits_nicks)} 处)")
    for h in hits_nicks[:20]:
        print(f"      {h}")

    # ---- 断言 2b:编译产物(*.pyc)全树扫描(含忽略路径) ----
    hits_pyc, n_pyc = _scan_compiled()
    ok2b = not hits_pyc
    print(f"[2b] 编译产物 .pyc 无敏感串(全树含忽略路径,扫描 {n_pyc} 个):"
          f"{'PASS' if ok2b else 'FAIL'}(命中 {len(hits_pyc)} 处)")
    for h in hits_pyc[:20]:
        print(f"      {h}")
    if not ok2b:
        failures.append(f"编译产物 .pyc 含敏感串 {len(hits_pyc)} 处")

    # ---- 断言 3:.gitignore 必须覆盖六类私有路径 ----
    print("[3] .gitignore 覆盖检查:")
    ok3 = True
    for target in _MUST_IGNORE:
        is_dir = target.endswith("/")
        rel = target.rstrip("/")
        covered = bool(GITIGNORE_PATS) and _is_ignored(rel, is_dir=is_dir)
        also = "(存在,已从扫描中排除)" if (ROOT / rel).exists() else "(当前不存在)"
        print(f"      {'PASS' if covered else 'FAIL'}  {target} {also}")
        if not covered:
            failures.append(f".gitignore 未覆盖 {target}")
        ok3 = ok3 and covered
    # 交叉验证:若上述路径真实存在,它们必须没进扫描集合
    for p, _ in files:
        rel = p.relative_to(ROOT).as_posix()
        if _is_ignored(rel, is_dir=False):
            failures.append(f"被 .gitignore 覆盖的文件混入扫描集: {rel}")

    if not ok1:
        failures.append(f"发现真实联系人缩写 {len(hits_initials)} 处")
    if not ok2:
        failures.append(f"发现真实联系人昵称 {len(hits_nicks)} 处")

    if failures:
        print(f"\nPRIVACY_FAIL: {len(failures)} 项未过: {failures}")
        print("!! 发布前必须修复;对照 README §十「发布到 GitHub 前须知」。")
        return 1
    print("\n全部检查通过:入库文本无真实联系人信息;.gitignore 覆盖口令/凭据/截图/日志。")
    print("PRIVACY_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
