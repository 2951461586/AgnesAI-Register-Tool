"""跨模块共享的顶层包。

目前只有 `config` 包（全局配置与启动校验）。它按职责拆成
`_env` / `site` / `mail` / `captcha` / `proxy` / `limits` 子模块，
`common/config/__init__.py` **只做转出**（`from common import config` 的写法不变）。

它被搬成顶层包（而不是留在 `src/` 里）是因为 `tools/` 下的脚本也要读配置，
而 `tools/` 与 `src/` 是**平级**的兄弟目录 —— 让 `tools/` 去 import `src.config`
会把整条 pipeline 的依赖链带进一个本该只读配置的脚本里。
"""
