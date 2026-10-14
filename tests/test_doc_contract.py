"""文档 / 配置的**机器可查**契约（零网络）。

为什么要有这个文件
==================
本项目的注释与文档密度很高（那是它的优点），但也因此反复出现同一类事故：
**文档承诺了一件事，而实现里没有**。已发生过的（都是本会话查出来的）：

  1. `export_keys.py` 的 docstring 写"`exports/` 在 `.gitignore` 里" —— 实际不在
     ⇒ 明文 `sk-` key 有泄漏面（P1）；
  2. `pyproject.toml` 提到"CI 的 secret-scan job" —— 仓库里**没有** `.github/`；
  3. `protocol.md §2` 写"Agnes 没有域名门" —— 实际有一道白名单；
  4. `protocol.md §4` 标题写"唯一的吞吐杠杆" —— 实际有两个（IP + 域名）。

这类事故的共性是：**它不会报错**，只会让人基于错的结论做决定（第 1 条甚至是
"人因为读过那句话而**不再检查**"）。所以这里把其中**机器可查**的部分固化成用例。

⚠ 本文件刻意只查"能机械判定"的事，不查"语义是否说得对"——
  后者只能靠人读。范围写小、判据写死，才不会变成一条会被 `# noqa` 掉的噪音。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 落**明文凭据**的目录 → 是否由**代码**写入。
#
# 🔴 两者都必须进 `.gitignore`：台账（邮箱+密码+JWT+sk- key）、配额/窗口状态、
#    `export_keys.py` 的导出（整把 sk- key）、备份（上面几样的副本）。
# ⚠ `backups/` 是**人工** `cp`/`mv` 出来的（没有代码写它）—— 所以它不参与下面那条
#    "反向守卫"，但它**同样**必须被忽略：里面就是台账的副本。
CREDENTIAL_DIRS = {
    "ledger": True,  # src/ledger.py
    "state": True,  # src/jsonlstore.py（配额计数 + 窗口事件）
    "exports": True,  # tools/data/export_keys.py
    "backups": False,  # 人工备份，不是代码写的
}


def _gitignore_patterns() -> set[str]:
    """`.gitignore` 里的有效模式（去掉注释与空行）。

    ⚠ 刻意**不调 `git check-ignore`**：那样用例就依赖"git 可用 + 有仓库"，
      而它本该在任何环境都能跑。直接解析文件更稳。
    """
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def test_credential_dirs_are_gitignored():
    """🔴 守卫：每个落明文凭据的目录都必须在 `.gitignore` 里。

    这条抓的正是 P1：`exports/` 曾经**不在**名单里，而 `export_keys.py` 的
    docstring 已经写着"在" ⇒ 一次 `git add -A` 就把整本 `sk-` key 提交进公开
    仓库，而人因为读过那句话**不会**再检查。

    ⚠ 判据用"目录名或 `目录名/`"两种写法都算 —— 本项目用的是带斜杠的目录形式。
    """
    pats = _gitignore_patterns()
    missing = [d for d in CREDENTIAL_DIRS if d not in pats and f"{d}/" not in pats]
    assert not missing, (
        f"这些目录落明文凭据，但不在 .gitignore 里：{missing}\n"
        f"  它们里面是邮箱/密码/JWT/sk- key/代理账密 —— 泄漏一次就够。\n"
        f"  修法：把它们加进 .gitignore（按目录排除，别靠 *.json 通配兜底）。\n"
        f"  ⚠ 新增落凭据的目录时，**先**加进 .gitignore 再写代码 —— 顺序很重要，\n"
        f"    因为写代码的时候没人会想起来加。"
    )


def test_credential_dirs_are_actually_written_by_the_code():
    """反向守卫：标了"代码写入"的目录，代码里必须真的写它。

    如果某天 `exports/` 不再被任何代码写到，它就该从名单里改掉/删掉 ——
    否则名单会慢慢变成"一堆没人认识的目录"，而那种名单没人会维护。

    ⚠ 只查标了 `True` 的（`backups/` 是人工备份，见 `CREDENTIAL_DIRS` 的注释）。
    """
    src = "\n".join(
        p.read_text(encoding="utf-8")
        for pkg in ("src", "common", "tools")
        for p in (ROOT / pkg).rglob("*.py")
    )
    for d, written_by_code in CREDENTIAL_DIRS.items():
        if not written_by_code:
            continue
        assert f'"{d}"' in src or f"'{d}'" in src, (
            f"`{d}` 标了「代码写入」，但代码里已经没有任何地方写到它了 —— "
            f"要么它被弃用了（改名单），要么路径改成了别的写法（更新名单）。"
        )


def test_ci_claim_implies_ci_config_exists():
    """🔴 守卫：说了"CI 会做 X"，就得真的有 CI 配置。

    实测事故：`pyproject.toml` 的注释写着"CI 的 secret-scan job 只装运行依赖"，
    而仓库里**没有** `.github/` —— 那句注释让人以为"密钥泄漏已经被自动挡住了"，
    于是不会自己去扫。**这比没有 CI 更坏**。

    ⚠ 判据按**段落**且排除否定句：说"目前**没有** CI 配置（没有
      `.github/workflows/`）"的段落是在**声明缺失**，不是在声称存在 ——
      不能把它也算成一次声称（本用例第一版就报了自己这段说明）。
    """
    claims = []
    for rel in ("pyproject.toml", "README.md", "docs/security-conventions.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for para in _paragraphs(text):
            if not re.search(r"CI 的|CI job|CI 会|CI 里|CI 上|workflows?/", para):
                continue
            # 否定句不算声称（允许 Markdown 粗体夹在中间：`没有** CI`）
            if re.search(r"(没有|尚无|暂无|未配|不带)[^。\n]{0,10}CI", para):
                continue
            claims.append(rel)
    if not claims:
        return  # 没人声称有 CI ⇒ 没什么可查的
    has_ci = any((ROOT / ".github" / "workflows").glob("*.y*ml"))
    assert has_ci, (
        f"这些文件声称有 CI：{claims}，但 `.github/workflows/` 下没有任何工作流。\n"
        f"  要么真的加上 CI（推荐：pytest + ruff + secret-scan），\n"
        f"  要么把那些措辞改成「本地手动跑」 —— 别让人以为它已经被自动挡住了。"
    )


def test_rate_limit_docs_point_at_the_single_source():
    """🔴 守卫：讲了限流旋钮的文件，必须指向 `docs/protocol.md` §3。

    背景：同一份知识写在四处**必然漂移**，而本项目真的漂移过 —— 窗口长度一度
    出现三个互不相同的值（`>4 分钟` / `>45 分钟` / 默认 `600s`），我因此在
    文档里写过两次错结论。现在 `protocol.md §3` 是唯一真源，其余文件只许**引用**。

    ⚠ 范围说明：本用例只保证"**引用了**"，不保证"引用的数字与真源一致"——
      后者要对中文散文做数字抽取，判据会变得又脆又难维护。所以这里是**结构**
      守卫：有旋钮的地方就得有指针。
    """
    knobs = ("MAX_PER_WINDOW", "WINDOW_S")
    offenders = []
    for rel in ("README.md", ".env.example", "common/config.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        if any(k in text for k in knobs) and "protocol.md" not in text:
            offenders.append(rel)
    assert not offenders, (
        f"这些文件讲了限流旋钮却没指向 `docs/protocol.md`：{offenders}\n"
        f"  限流事实的唯一真源是 `docs/protocol.md` §3 —— 引用它，别在那里另写数字。"
    )


def test_protocol_md_still_has_the_sections_others_reference():
    """被引用的锚点必须真的存在。

    ⚠ 多个文件写"见 `docs/protocol.md` §3"，如果哪天那一节被改名/删掉，
      所有引用就变成空指针 —— 而空指针不会报错，只会让人找不到东西。
    """
    text = (ROOT / "docs" / "protocol.md").read_text(encoding="utf-8")
    headings = [ln.strip() for ln in text.splitlines() if ln.startswith("#")]
    for num, keyword in (("3", "限流"), ("2", "端点"), ("4", "出口"), ("6", "判据")):
        hit = [h for h in headings if h.startswith(f"## {num}.") and keyword in h]
        assert hit, (
            f"别的文件引用 `docs/protocol.md §{num}`（{keyword}），"
            f"但那一节不存在或已改名。现有标题：{headings}"
        )


def test_env_example_lists_every_knob_the_code_reads():
    """`.env.example` 是用户唯一的配置入口 ⇒ 它不能漏掉代码会读的变量。

    实测踩过：`AR_MAIL_DOMAINS` / `AR_LIMITER_MAX_WAIT_S` 这类旋钮在
    `.env.example` 里缺席或只在注释里出现，用户根本不知道它存在。
    """
    code = (ROOT / "common" / "config.py").read_text(encoding="utf-8")
    read = set(re.findall(r'os\.getenv\("(AR_[A-Z_]+)"', code))
    read |= set(re.findall(r'_env_(?:int|float|bool)\("(AR_[A-Z_]+)"', code))
    assert read, "没从 config.py 里抽出任何 AR_* 变量 —— 本用例的前提变了，请重看"

    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    missing = sorted(v for v in read if v not in example)
    assert not missing, (
        f"这些变量代码会读，但 `.env.example` 里一个字都没提：{missing}\n"
        f"  用户唯一的配置入口就是它 —— 没写等于「这个旋钮不存在」。"
    )


def test_every_module_is_documented():
    """🔴 守卫：新增模块必须在**两个索引**里登记。

    实测漂移：`jsonlstore.py` / `windowstore.py` 加进来之后，README 的目录树与
    `src/__init__.py` 的模块分工都没跟上 —— 而那两处是新人（与人）了解"这个项目
    有哪些模块"的**唯一入口**。漏登不会报错，只会让人找不到东西。

    ⚠ 判据是**子串**（模块名 stem），所以 `src/{a,b,c}.py` 这种合并写法也算数。
    ⚠ 每个模块查**它自己那个包**的 `__init__.py`：`config` 在 `common/` 下，
      就不该指望 `src/__init__.py` 里出现它；`pipeline/limiter.py` 同理该在
      `src/pipeline/__init__.py` 里登记。
    ⚠ 必须**递归**扫（`rglob`）：P5 把 `src/pipeline.py` 拆成了包，用 `glob` 的话
      5 个子模块会整个跳过 —— 而那正是本用例要防的"新东西没人登记"。
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    mods = [
        (p, p.parent / "__init__.py")
        for pkg in ("src", "common")
        for p in sorted((ROOT / pkg).rglob("*.py"))
        if p.stem != "__init__"
    ]
    assert mods, "没扫到任何模块 —— 本用例的前提变了"
    for p, init in mods:
        rel = p.relative_to(ROOT).as_posix()
        irel = init.relative_to(ROOT).as_posix()
        assert p.stem in readme, f"模块 `{rel}` 没在 README 的目录里登记"
        assert init.is_file(), f"`{rel}` 所在目录没有 `__init__.py`"
        assert p.stem in init.read_text(encoding="utf-8"), (
            f"模块 `{rel}` 没在 `{irel}` 的模块分工里登记"
        )


def test_exit_code_table_matches_run_py_docstring():
    """README 的退出码表必须与 `run.py` 的 docstring 一致。

    实测漂移：P0 把"缺必需配置"改成"配置有问题"（因为 `validate()` 现在也报
    "值不合法"），而 README 的表格没跟上 —— 用户照表排错会找错方向。

    ⚠ 只比**码**（0~5 是否都在两处出现），不比文案（文案本来就会演进）。
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    run_py = (ROOT / "run.py").read_text(encoding="utf-8")
    for code in range(7):
        assert re.search(rf"\| {code} \|", readme), f"README 的退出码表缺 {code}"
        assert re.search(rf"^  {code} =", run_py, re.M), f"run.py 的 docstring 缺退出码 {code}"


def _paragraphs(text: str):
    """按空行切段。

    ⚠ 为什么需要它：中文文档里一句话常常折成好几行，所以"这一行是不是在讲
      漂移历史"这种判断**必须按段落做** —— 按行做会把折行的后半行误判成
      "在断言那个数字"（本用例第一版就踩了这个坑，把自己的解释行报了出来）。
    """
    buf: list[str] = []
    for line in text.splitlines():
        if line.strip():
            buf.append(line)
        elif buf:
            yield "\n".join(buf)
            buf = []
    if buf:
        yield "\n".join(buf)


def test_no_secret_shapes_in_shipped_files():
    """🔴 守卫：**会被提交的文件**里不能出现凭据形状。

    为什么需要它（本项目特别需要）：仓库是**公开**的，而整条链路都在处理明文
    凭据 —— `.env` 的 Worker token、`ledger/` 的邮箱+密码+JWT+`sk-` key、
    `.slots.txt` 的代理账密。它们都在 `.gitignore` 里，但"配了忽略"挡不住
    `git add -f`，也挡不住有人顺手改了 `.gitignore`（P1 就是后者）。

    与 CI 里 gitleaks 的分工：这个是**零依赖 + 离线 + 每次 pytest 都跑**，
    覆盖面小但永远在；gitleaks 覆盖面大（全历史 + 更全规则集）但只在 CI 里。

    ⚠ 判据只收**高置信度**的形状，并刻意放过两类东西：
      · 文档里的占位符（`sk-XXXX…`、`host:port:user:pass`）；
      · **RFC 2606 / RFC 6761 保留域名**（`.example` / `.test` / `.invalid` /
        `localhost`）—— 它们**不可能**是真实凭据，而用例故意拿它们当样本
        （如 `tests/test_redact.py` 需要一条**形状像**代理串的假串来验脱敏）。
      宁可少报，也不要变成一条被 `# noqa` 掉的噪音。
    """
    # 保留给文档/测试的域名：出现在命中里就说明那是合成样本，不是真凭据。
    reserved = (".example", ".test", ".invalid", "localhost", "example.com", "example.org")
    shapes = {
        "私钥": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "JWT": re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}"),
        # 真实 sk- key 是 48 位左右的字母数字；文档里写的是 `sk-XXXX…` 这类
        # 带省略号/尖括号的占位符，不会命中。
        "sk- key": re.compile(r"\bsk-[A-Za-z0-9]{24,}\b"),
        # `host:port:user:pass`（代理串的第二种形态）。
        # 端口要求是 2~5 位数字，所以文档里的 `host:port:user:pass` 占位符不命中。
        "代理账密串": re.compile(
            r"\b[a-z0-9][a-z0-9.\-]{3,}:[0-9]{2,5}:[A-Za-z0-9_\-]{6,}:[A-Za-z0-9_\-]{6,}\b"
        ),
    }
    files = [
        ROOT / f
        for f in (
            "README.md",
            ".env.example",
            ".gitignore",
            "pyproject.toml",
            "requirements.txt",
            "run.py",
        )
    ]
    for pkg in ("src", "common", "tools", "tests", "docs"):
        files += sorted((ROOT / pkg).rglob("*"))

    hits = []
    for p in files:
        if not p.is_file() or p.suffix in (".pyc", ".gz"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name, rx in shapes.items():
            m = rx.search(text)
            if m and not any(r in m.group(0) for r in reserved):
                hits.append(f"{p.relative_to(ROOT).as_posix()}：命中「{name}」→ {m.group(0)[:40]}…")

    assert not hits, (
        "这些**会被提交**的文件里出现了凭据形状：\n  "
        + "\n  ".join(hits)
        + "\n  修法：把它移到 `.gitignore` 覆盖的目录（`ledger/` / `state/` / "
        "`exports/` / `backups/`）或 `.env`，\n"
        "  ⚠ 如果它已经进过 git 历史，仅仅删掉文件**不够** —— 历史里永远还在。"
    )


def test_no_bare_measured_window_value_outside_protocol():
    """实测的窗口下界只许出现在 `docs/protocol.md`。

    ⚠ 允许两种例外，都是**故意的**：
      · 讲"漂移历史"的**段落**（提到"漂移"就是在引用过去写错的值，不是断言事实）；
      · `protocol.md` 自己。
    判据按**段落**而不按行 —— 见 `_paragraphs` 的注释。
    """
    needle = "45 分钟"
    offenders = []
    for rel in ("README.md", ".env.example", "common/config.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for para in _paragraphs(text):
            if needle in para and "漂移" not in para:
                offenders.append(f"{rel}: {para.strip()[:80]}")
    assert not offenders, (
        "实测的窗口下界被写到了 protocol.md 之外（除「漂移历史」段落）：\n  "
        + "\n  ".join(offenders)
        + "\n  修法：改成引用 `docs/protocol.md` §3 —— 那个数字只在那边维护。"
    )
