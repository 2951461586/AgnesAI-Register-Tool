"""本地闸门：滑动窗口、域名预留、限流器工厂、换出口。

🔴 **本文件只做转出**：`from src.pipeline import X` / `from .limiter import X`
   的既有写法保持不变。实现按职责拆在三个子模块里：

     window.py       滑动窗口原语（SlidingWindow / WindowBusy）
     reservation.py  域名预留（DomainReservation）
     registry.py     限流器工厂 + 按维度挑桶 + 换出口（策略）

⚠ 拆分是**纯搬移**（逐字），没有改任何逻辑。用例是这次搬移的安全网，
  其中包含本会话两个 bug 的回归用例（并发烧码、网络失败扣 0 格）。
"""

from common import config

from .registry import (
    _DOMAIN_LIMITERS,
    _DOMAIN_USED,
    _LIMITER_LOCK,
    _REGISTER_LIMITERS,
    _is_domain_limit,
    _is_ip_limit,
    _pick_domain_with_room,
    _register_backoff_s,
    _rotate_slot,
    _slot_has_room,
    _slot_key,
    domain_limiter,
    pick_domain,
    register_limiter,
)
from .reservation import DomainReservation
from .window import SlidingWindow, WindowBusy

# 一个账号要打几枪**注册类**请求。
#
# 🔴 **真源在 `common/config/limits.py`**（那里有完整的量化表：MAX=3 → 1 个账号）。
#    这里只**转出**，不另写一个 `2`：`config.validate()/lint()` 要用它判断
#    "窗口是不是连一个账号都装不下"，而 `common/` 是更低的层、不能反向 import
#    `src/`。同一个事实写两处必然漂移，而这里的漂移形态是"静默少一半容量"。
REQUESTS_PER_ACCOUNT = config.REQUESTS_PER_ACCOUNT
