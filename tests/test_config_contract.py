"""配置契约的离线用例（零网络）。

钉住三件事：
  1. **窗口上限按 `REQUESTS_PER_ACCOUNT` 量化** —— `MAX < 2` 等于**永远跑不出
     账号**（`floor(MAX/2) == 0`），这是**静默失效**，必须被 `validate()` 拦下；
  2. 奇数上限会**静默**丢掉最后一格（`MAX=3` 看着像 3 个账号、实际 1 个），
     由 `lint()` 提醒 —— 但**不**硬拦（能跑，只是浪费）；
  3. `REQUESTS_PER_ACCOUNT` 只有**一个真源**（`common/config.py`），
     `src/pipeline.py` 是转出。

为什么值得单独一个文件：这两个数字（`MAX_PER_WINDOW` 与"一个账号要几枪"）
在 2026-10-14 被**读错过**（"2 次" → "2 个账号"），而误读的后果不是报错，
是"码烧了、注册进不去"。把单位关系变成可执行断言，比再写一遍注释可靠。
"""

from common import config
from src import pipeline


def _max_entries(items):
    """只挑出与窗口上限有关的条目（`validate()` 还会报 Worker 凭据等）。"""
    return [x for x in items if "MAX_PER_WINDOW" in x]


def test_requests_per_account_has_a_single_source():
    """🔴 `pipeline` 只**转出** `config` 的值，不另写一个 2。

    同一个事实写两处必然漂移，而这里的漂移形态是"静默少一半容量"。
    """
    assert pipeline.REQUESTS_PER_ACCOUNT == config.REQUESTS_PER_ACCOUNT
    assert config.REQUESTS_PER_ACCOUNT == 2


def test_window_smaller_than_one_account_is_rejected(monkeypatch):
    """🔴 守卫：`MAX < REQUESTS_PER_ACCOUNT` ⇒ **0 个账号**，必须在启动阶段拦下。

    这是最坏的一类配置错误：不报错、不告警，只是每个账号都被本地闸门 skip，
    现象看着像"域名全被限流"（实测踩到过类似的误导）。
    """
    monkeypatch.setattr(config, "REGISTER_MAX_PER_WINDOW", 1)
    monkeypatch.setattr(config, "DOMAIN_MAX_PER_WINDOW", 1)
    bad = _max_entries(config.validate())
    assert len(bad) == 2, bad
    assert all("永远跑不出账号" in b for b in bad), bad


def test_odd_window_warns_but_does_not_block(monkeypatch):
    """奇数上限：**能跑**（所以不拦），但会静默浪费最后一格 ⇒ `lint()` 提醒。"""
    monkeypatch.setattr(config, "REGISTER_MAX_PER_WINDOW", 3)
    monkeypatch.setattr(config, "DOMAIN_MAX_PER_WINDOW", 3)

    assert _max_entries(config.validate()) == []  # 不硬拦
    warn = _max_entries(config.lint())
    assert len(warn) == 2, warn
    # 必须说清"实际能跑几个账号"与建议值 —— 只说"奇数不好"没用
    assert all("= 1 个账号" in w for w in warn), warn
    assert all("建议改成 2 或 4" in w for w in warn), warn


def test_even_window_is_silent(monkeypatch):
    for v in (2, 4, 6):
        monkeypatch.setattr(config, "REGISTER_MAX_PER_WINDOW", v)
        monkeypatch.setattr(config, "DOMAIN_MAX_PER_WINDOW", v)
        assert _max_entries(config.lint()) == [], v
        assert _max_entries(config.validate()) == [], v


def test_lint_does_not_repeat_validate_with_a_silly_suggestion(monkeypatch):
    """🔴 `MAX < 2` 时 `lint()` **不能**再报一次。

    否则它会给出"建议改成 0" —— 0 格等于关掉闸门，是个**荒谬**的建议，
    而一条荒谬的建议会让人不再信任整个 lint。
    """
    monkeypatch.setattr(config, "REGISTER_MAX_PER_WINDOW", 1)
    monkeypatch.setattr(config, "DOMAIN_MAX_PER_WINDOW", 1)
    assert _max_entries(config.lint()) == []
    assert "建议改成 0" not in " ".join(config.lint())


def test_default_config_is_clean():
    """默认值（2 次 = 1 个账号）既不该被拦、也不该有警告。"""
    assert _max_entries(config.validate()) == []
    assert _max_entries(config.lint()) == []
