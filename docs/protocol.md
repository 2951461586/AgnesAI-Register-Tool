# Agnes AI 注册链路：协议与实测

> 本文件是**现行文档**：动注册 / 登录 / 建 Key / 限流相关代码之前先读它。
> 所有结论都来自**线上实测**（2026-10-13 起，单一出口 `203.0.113.10`），
> 不是从混淆 JS 里猜的。凡是**没测到**的，都会明确写“未测”——
> 把猜测写成结论是这个项目最不能犯的错。
>
> 🔴 **2026-10-15 追加了一条推翻前提的发现**：②发验证码 开始要求 **captcha**
> （见 §2②）。在那之前“零浏览器”成立；现在 ② 多了一步 headless captcha。

---

## 1. 全链路（6 步）

```text
① 建邮箱        CF Worker  POST /api/mailboxes            → {"emails":[...]}
② 发验证码      Agnes      GET  /api/verification?email=&purpose=register
                                                          → {"code":200,"message":"ok"}
③ 收验证码      CF Worker  GET  /api/inbox?email=          → extracted_type="code"
④ 注册          Agnes      POST /api/user/register         → data.access_token + data.user
⑤ 密码登录      Agnes      POST /api/user/login            → data.access_token（新 JWT）
⑥ 建 API Key    Agnes      POST /api/token                 → data.key = "sk-..."
   只读校验      Agnes      GET  /api/user/self | balance | subscription | token
   Key 可用性    网关       GET  /v1/models + POST /v1/chat/completions
```

**2026-10-15 16:24 之前**，整条链路没有验证码图片、没有 WAF 解盾、没有浏览器，
运行依赖只有 `requests`（对比 Intern-Register-Tool 必须用真浏览器解阿里云
验证码）。🔴 **16:24 起 ②发验证码 要求 captcha** ⇒ 这一枪 `400`、链路在此断掉：
实测原文、复测与**未测项**见 **§2②**。

---

## 2. 端点与响应形状（实测原文）

### ② 发验证码

```http
GET https://platform-backend.agnes-ai.com/api/verification?email=<addr>&purpose=register
x-user-language: zh-CN
Origin: https://platform.agnes-ai.com
Referer: https://platform.agnes-ai.com/
```

```json
{"code":200,"message":"ok","data":{"email":"<addr>"}}
```

🔴 **2026-10-15 16:24 起：这一步开始要求 captcha**（实测原文）：

```json
HTTP 400 {"code":400,"message":"Captcha verification is required"}
```

- **首次观测** 2026-10-15 16:24:49：台账全量 209 条里只有那一批的 **7 条**命中，
  之前**一条都没有**。**复测**（16:3x，单独打一枪、走本地闸门记账）：仍然是
  同一条 400，**2.6s** 返回 ⇒ 不是瞬时抖动。
- 卡在**发码**这一枪（`stages.code`），而且发生在**第一次**调用 —— 失败记录的
  `timings` 只有 `mailbox_ms`、**没有** `code_send_ms`（那个键只在重试循环跑完
  之后才写）⇒ 不是重试耗尽，是直接拒。
- 分类落在 **`rejected`**（服务端明确拒绝）—— **不是** `quota`、也不是 `network`。
  判据见 `src/agnes.py` 的 `_request`（4xx → `AgnesRejected`）。

### captcha 的形态与过法（2026-10-17 用**完整 HAR** 钉死）

🔴 结论：**腾讯防水墙 TCaptcha**（**不是** Cloudflare Turnstile / hCaptcha），
形态是**点选**（按提示顺序点 N 个图标）—— **不是**拖动，也**不是**无感。

证据来源：一份 2026-10-17 的**完整成功注册 HAR**（132 条请求，直连、
非槽位池），抓到了 `prehandle → 两张图 → verify → ticket → /api/verification`
的全程。下面每个字段都是**实测原文**，不是从 bundle 猜的。

- 脚本：`ca.turing.captcha.qcloud.com/TCaptcha-global.js`
  （`NEXT_PUBLIC_TENCENT_CAPTCHA_REGION="global"` ⇒ 走 global 那一支；
  CN 支是 `turing.captcha.qcloud.com/TJCaptcha.js`）。
- **appid `189979000`**（`new window.TencentCaptcha("189979000", cb)`）。
- 回调给的是 **`{ret, ticket, randstr}`** 一对值。
- 🔴 **传参方式**：发码那一枪（`/api/verification`，GET）带上 **`ticket` +
  `randstr`** 两个参数 —— 前端原文：`x.aT({email, purpose, ticket, randstr})`。
  **其余端点不带**：HAR 里 `/api/user/register`、`/api/user/login`、`/api/token`
  的 body/query 都**没有**这两个参数 ⇒ 这道门**只装在发码上**（已实测确认，
  不再是"从前端看大概率不要"）。
- ⚠ 本工具目前只发 `email` + `purpose` ⇒ 必被拒。这就是那条 400 的直接原因。

#### 完整流程（HAR 实测）

```text
GET  cap_union_prehandle?aid=189979000&showtype=popup&entry_url=.../login
       ← sess + dyn_show_info + comm_captcha_cfg.pow_cfg
GET  tdc.js?app_data=...                    ← 设备指纹 JS（eks 的来源）
GET  cap_union_new_getcapbysig?img_index=0  ← 提示图 170×50（要点的图标与顺序）
GET  cap_union_new_getcapbysig?img_index=1  ← 背景图 672×480（目标 + 干扰项）
POST cap_union_new_verify
       collect=<1752字符> tlg=<长度守卫> eks=<236字符> sess=<514字符>
       ans=<明文坐标数组> pow_answer=<prefix+N> pow_calc_time=<秒>
       ← {"errorCode":"0","randstr":"@xxxx","ticket":"tr...","errMessage":""}
GET  /api/verification?email=&purpose=register&ticket=tr...&randstr=@xxxx
       ← 200 ok
```

#### 点选：`ans` 是**明文**坐标数组

`dyn_show_info` 里明确写着（HAR 原文）：

```json
{"instruction":"请依次点击：",
 "bg_elem_cfg":{"size_2d":[672,480],
   "click_cfg":{"mark_style":"inc_number","data_type":["DynAnswerType_POS"]},
   "img_url":"/cap_union_new_getcapbysig?img_index=1&..."},
 "ins_elem_cfg":[{"id":1,"sprite_pos":[0,0],"size_2d":[170,50]}]}
```

`verify` 里的 `ans` 长这样（HAR 原文，3 个点）：

```json
[{"elem_id":1,"type":"DynAnswerType_POS","data":"39,272"},
 {"elem_id":2,"type":"DynAnswerType_POS","data":"266,260"},
 {"elem_id":3,"type":"DynAnswerType_POS","data":"351,187"}]
```

⇒ **答案格式极简**：按顺序点的 N 个 `(x,y)`，坐标落在 `size_2d` 内。
难的不是格式，是**从背景图里认出提示图那 N 个图标在哪**。

⚠ 这一步**已做出原型并跑通 live**：`.workbuddy-ai/captcha/solve_click.py`
（探针侧、不进 `src/` 的运行依赖）用「纯黑掩码 + 清理提示图噪点 + 多尺度/旋转
NCC + 匈牙利分配」，在 HAR 样本上 **3/3 分配正确**。

#### 2026-10-17 live 实测：headless 全链路已跑通

在 **headless** Chrome 下用 `probe_captcha.py --auto-solve` 跑通了一次完整链路：

```text
点选 3 图标（solver ~6.2s）→ 点击 3/3 → .tc-action.verify-btn
→ cap_union_new_verify: errorCode=0
→ ticket=tr03... / randstr=@WXZ
→ /api/verification 200 {"code":200,"message":"ok"}
```

关键实测值：

- **点击映射**：图像坐标 → `.tc-imgarea` 的 `bounding_box`（本次显示 340×243，
  缩放 0.506×0.506）。widget 记录的 `ans` 与我们的点击点差 **~11px**（它会吸附），
  服务端**接受**（`errorCode=0`）⇒ 容差 ≥ 11px。
- **确认按钮**：popup 模式要的是 **`.tc-action.verify-btn`**（带 `show` 类），
  不是 `.verify-btn` —— 后者会先命中 embed 模式的隐藏按钮（实测踩过）。
- **PoW**：本次 `N=50089`，Python **43.6ms**（比 HAR 样本的 132 难，但仍毫秒级）。
- **headless 可用**：本次没有触发无头检测；`tdc.js` 的 `eks` 由页面自己生成。

⚠ **2026-10-19：solver 内部已重写（P1–P6）** —— 上面的 `~6.2s` 是重写前的
live 值。合成语料（16 张、每张 5 个候选）上单题 **12.2s → 2.2s**，命中率
**10/16 不变**；真实题图候选最多 30 个，收益预计更大。**live 墙钟尚未复测**。

⚠ **solver 的准确率随题变化**：3 张 live 样本里 2 张 **2/3**、1 张 **3/3**。
最弱的是「2」字形 —— 它容易被圆形 smiley / 眼镜镜片抢走（NCC 上圆形弧线
比被遮挡的「2」更像模板）。本次成功的题里 3 个图标都清晰、分数 0.76~0.83。

#### PoW：`md5(prefix + N) == target`，纯 Python 可秒解

`prehandle` 的 `comm_captcha_cfg.pow_cfg`：

```json
{"prefix":"<16位hex>#","md5":"<32位hex>"}
```

`verify` 里的 `pow_answer` = `prefix + str(N)`，且
**`md5(pow_answer) == pow_cfg.md5`**（HAR 样本 `N=132`，JS 侧
`pow_calc_time=2` 秒；Python 暴力搜毫秒级）。
⇒ 这一层**不需要浏览器**，是纯计算。

#### 真正的壁垒：`collect` + `eks`

- `collect`（HAR 里 1752 字符）：点击轨迹 / 行为加密数据，由 `dy-ele.js` 生成；
- `eks`（236 字符）：TDC 设备指纹，由混淆过的 `tdc.js`（`__TENCENT_CHAOS_VM`）生成；
- `sess`：`prehandle` 下发，一次性。

这三个是**混淆 JS 的运行时产物**，无法从 HAR 推导，也无法用纯 `requests` 伪造
（HAR 里的 `sess`/`ticket` 都已消费、且绑定当次指纹与 IP，**不可重放**）。

#### 适配路线（按可行性）

1. **真浏览器（Playwright/Selenium）+ 自动点选**：让页面 JS 自己生成
   `collect/eks/sess/PoW`，我们只负责「从背景图认出 N 个图标 → 按顺序点击」，
   再从回调取 `ticket/randstr`。HAR + **2026-10-17 headless live** 已证明
   这条路能走通（`verify errorCode=0` → `/api/verification 200`）。
   代价：+Playwright 重依赖 + 无头指纹检测风险（`tdc.js` 会采集环境）。
2. **打码平台**：把图发给平台拿坐标 —— 但**仍要解决 `collect/eks`**，
   除非平台直接代过整个 TCaptcha；且很可能违反服务条款。
3. **纯 Python 复刻**：PoW 和 `ans` 都能做，但 `collect/eks` 是混淆 VM ⇒ 不现实。
4. **前端降级路径**：见下面"仍未测清"。

⚠ **仍未测清**（别当结论用）：

- **是全局要求，还是按量 / 按时段 / 按出口触发？** live 只跑了几次（直连、
  非槽位池），它证明「至少这几次是交互式点选」，**不能**证明「住宅 IP 就无感」，
  也不能证明「所有出口都要」。此前观测到的那一批里前 4 个账号成功、之后 7 个
  全被拦（跨 7 个域名、7 个不同出口），与「按量升级」仍**区分不了**。
- 🔴 前端里有一条**降级路径**，值得单独测一次：captcha 脚本**加载失败**时
  前端**自己造**一对值发出去 —— `ticket="trerror_1001_189979000_<unix秒>"`、
  `randstr="@"+随机串`（原文函数 `N()`，`errorCode:1001,
  errorMessage:"jsload_error"`）。⇒ **服务端认不认这对假值，未测**。
  认 ⇒ 不用浏览器也能过；不认 ⇒ 只能上真浏览器 / 打码。
  ⚠ 用它是**主动绕过**风控，且很可能违反服务条款。
- **图标识别的泛化仍未测**：HAR 的背景图是照片 + 黑色线稿图标，提示图是
  同一批图标的缩略图；两者**尺寸/旋转都不固定**（实测见过 0.4×、180°、
  被眼镜遮挡的「2」）。**单次求解成功率约 50%**（下面「出口与准确率」的实测），
  失败集中在**细长/被遮挡字形**（「1」「2」「日」）被配到**密集复合图标**上。
  ⇒ 换图标集 / 换背景的准确率**未测**。

#### 出口与准确率（2026-10-17 实测，`tools/probes/probe_captcha_egress.py`）

两个问题分开测，结论都推翻了原来的保守假设：

| 问题 | 实测 | 结论 |
| --- | --- | --- |
| 浏览器走不走代理，影响**成功率**吗 | 直连 1/2、代理 1/2（失败都是 `errorCode=50`） | **不影响**。失败是 solver，不是链路 |
| 影响**耗时**吗 | 直连 23.0s / 26.0s；代理 49.4s / 54.2s | **直连快一倍** ⇒ 默认 `AR_CAPTCHA_USE_PROXY=0` |
| `uip` 不一致会被拒吗 | 2026-10-17：直连解 ticket → **代理**发码：`200 {"code":200,"message":"ok"}`；2026-10-18 复测 **2/2 接受**（第 3 个样本 captcha 没解出来） | **没有被拒**（3 次里 2 次有效、全接受）⇒ **不**需要强制同一出口 |

⚠ 上表第 3 行在 2026-10-17 只有**一个**样本；2026-10-18 用
`probe_captcha_egress.py --uip-test --uip-runs 3` 复测到 **2/2 接受**（第 3 个
样本是 captcha solver 的 `errorCode=50`，不是 uip 拒绝）。样本仍小（n=2），
所以结论保持为"**到目前为止没被拒**"，不是数学证明。现在默认直连（快一倍），
但 `AR_CAPTCHA_USE_PROXY=1` 保留旧行为；若哪天发码开始报
`Captcha verification is required`，第一件事就是把它打开重测。

降级策略（按实测定的）：**一次 45s 超时 → 换出口重试一次**，不再在同一个出口上
重开浏览器。旧行为（90s × 2 次）实测 **267s/账号**，而新策略下整批中位 **~45s**。
live 5 批的成功数：`2/5`（旧 solver）→ `1/5`（新 solver + 代理 captcha）→ **`3/5`**（新 solver + 直连 captcha）。

⚠ **HAR 本身含明文凭据**（账号密码 / JWT / `sk-` key / 出口 IP / 会话令牌）
⇒ **不要提交进仓库、不要贴 issue**。本文只引用形状，不引用值。

🔴 **现状（2026-10-17）**：主流水线已接入可选 captcha provider（`src/captcha.py`，
**默认开、headless**）：发码前先解出 `ticket`/`randstr`，再走原来的 `requests`。
`src/` 的运行依赖变成 `requests` + **可选** `[captcha]` extra；不装就
`AR_CAPTCHA_ENABLED=0`（但发码会被服务端 400 拒绝）。provider 失败归
`error_kind="captcha"`（**不是** `rejected`）。

✅ HAR 同时证明：**过了这道门之后，`/api/user/register`、`/api/user/login`、
`/api/token` 全部 200** ⇒ 链路其余 5 步的协议**没有变**，缺的只是发码那一枪的
`ticket/randstr`。

- `purpose=register` 是实测用的取值（注册流程）。
- **不存在的域名也返回 200**（拿 `@example.com` 试过）—— 服务端不在这一步
  做域名校验，所以这一枪"成功"不代表能收到信。
- ⚠ **"没有域名门"是错的说法**（本节曾这么写）。准确说法是：**没有"自有域名"
  这道门**（`@own.example` 这类自有域名直接过，不像 OpenXLab 的 `A0232` 需要
  报备），**但平台侧有两道检查** —— 而且**返回两条不同的消息**：

  | 消息 | 含义 | 实测命中 |
  | --- | --- | --- |
  | `This email domain looks invalid or abusive and is not allowed for registration` | 域名信誉 / 滥用名单 | `own4.example`、`own5.example`、`own6.example`、**`outlook.com`** |
  | `Disposable or temporary email addresses are not allowed for registration` | **一次性邮箱名单** | `1secmail.com`、`mailinator.com`、`dropmail.me` |
  | （放行） | | `own.example`、`sub.own.example`、`own2.example`、`own3.example`、`gmail.com`、`example.com`、`probe.own.example`、`catchmail.io`、`mailistry.com`、`zeppost.com`、`fengyou.cc`、`nutemail.com` |

  🔴 **第二条消息是 2026-10-14 才发现的**（此前只知道第一条）。它意味着
  **第三方临时邮箱服务基本都没戏** —— 实测的 3 个知名服务全被它拦下。
  （同类项目也记着"约 70 个常见临时邮箱域名被拒，且**动态更新**"。）

  🔴 **`outlook.com` 被拒是反直觉的**（大厂邮箱），但在**两个不同出口**上
  各测一次结果一致。合理推测：`outlook.com` 正是"账号池 / 接码平台"的重灾区，
  所以进了滥用名单。⇒ **不要**把接码平台买的 `@outlook.com` 当作可用域名。

  🔴 **域名数是吞吐的硬上限**（`DOMAIN_MAX_PER_WINDOW` 格 = MAX/2 个账号）⇒ 可用域名数直接决定
  每小时能跑几个。把被拒的域名加进域名池只会拿到确定性的 `rejected`，
  **还会白烧一格 IP 窗口** —— 没有收益，只有损失。

  ⚠ 白名单是**平台侧**的、随时可能变；换池前先跑
  `tools/probes/probe_domains.py`（⚠ 消耗注册窗口）。

  ⚠ `own3.example` 首测（10-13）记为"未测清" —— 当时撞到的是 **IP 限流**，
  不是域名拒绝。10-14 复测确认**允许**。**"未测清" ≠ "被拒"**：把限流当成
  域名被拒，就会白白丢掉一个可用域名。

### 加域名窗口的两条路（以及为什么只有一条可靠）

**域名数决定吞吐**，所以"多一个邮箱源"是唯一能加域名窗口的手段。实测两条路：

| 路 | 结果 |
| --- | --- |
| **自有域名**（在 CF Worker 上加） | ✅ 唯一**可靠**的路：独占窗口、可预测。加完之后用 `probe_domains.py` 验白名单 |
| **第三方临时邮箱服务** | ⚠️ 只有**没被列入名单**的能用。实测 `catchmail`（3 域名）与 `smailr`（4 域名）可用；`1secmail` / `mailinator` / `dropmail` 全被拒；`outlook.com` 系（接码平台）被拒 |

🔴 **第三方源的关键代价**：它的域名是**公共**的 —— 任何人都能在上面建地址
⇒ 那个域名的服务端窗口**与陌生人共享**，可能被别人的量打满且**不可控**。
所以它的定位是**补充窗口**，不是替代自有域名。
（同类项目就记过 `Mail.tm` 的公共域名 `uberip.com`"周期性被封"。）

⇒ 本项目因此支持**多源**（`AR_MAIL_SOURCES`，默认只有 `worker`）：
自有域名当主力，`catchmail` / `smailr` 当溢出容量（`smailr` 给 4 个域名，
是三个源里最值的一个）。详见 `src/mailbox.py`。

#### 子域是不是独立窗口（2026-10-18 重新测）

🔴 **旧结论的理由作废，但结论（独立）被实测确认。** 旧文档写"子域很
可能按**精确 hostname** 计"，依据是"`own.example` 与 `sub.own.example` 同时
成功 ⇒ 合计 4 次会超阈值（2~3）"。但 2026-10-18 实测发现：**域名阈值不是
2~3，而是 ≈10**，所以"4 次没超"什么也证明不了 —— 旧结论是**碰巧对了**。

新的受控实测（`tools/probes/probe_window_limits.py`）：

| 实验 | 结果 |
| --- | --- |
| 同一域名、**不同出口**打 3 枪 | 全过 ⇒ 阈值 > 3 |
| 同一域名、**同一出口**打 3 枪 | 全过 ⇒ 阈值 > 3（所以不是 2~3） |
| **全新域名**、**10 个不同出口**连打 10 枪 | 前 9 枪过，**第 10 枪** `Too many registration attempts from this email domain` ⇒ **阈值 ≈10 / 窗口**，且**跨出口计数** ⇒ 域名窗口是**全局**的 |
| 历史台账（2026-10-14 02:06~02:17） | 14 次域名限流里 **13 次**在 ±5 分钟内"父/子另一边也被域名限流"；只有 1 次另一边在 157s 后发码成功 |

⇒ 要测"子域是否独立"，必须先把**父域打满 ~10 枪**，再立刻探子域：

- 子域成功 ⇒ 独立（按精确 hostname）；
- 子域也被域名限流 ⇒ **共窗**（按可注册域）。

⚠ 历史台账那 13/14 次"两边同时限流"**不能**当共窗证据：当时 4~5 路生产者
同时打这两个 hostname，两边各自撞自己的阈值，时间上自然接近。受控实验
（一次只打一边、打满后立刻探另一边）才是判据。

⚠ 受控复测（2026-10-18，`own.example` / `sub.own.example`）：父域第 7 枪被
`Too many registration attempts from this email domain` 拒后，**换一个出口**
立刻给子域发码 → `200 ok`。结合 §3 的"10 个不同出口一起计数"（域名窗口是
**全局**的，换出口绕不过去）⇒ **子域是独立窗口（按精确 hostname 计）**。
⚠ 这次父域第 7 枪就打满（而不是 10 枪），是因为 43 分钟前的 3 枪仍在窗口里
—— 见 §3 的"多级限流"讨论；它不影响"子域独立"这个结论。
（⚠ 显示出来的槽位串相同是因为 `redact_url` 把 lajiao 的 sid 抹掉了；实际
用的是**另一个 full slot**。同一出口下**无法**复测：IP 阈值 ≈4 < 域名阈值
≈10，一个出口打不满域名窗口 —— 但域名计数是全局的，所以跨出口的结论成立。）

⇒ 本地闸门**按精确 hostname 分桶**与实测一致（同父域的子域各有一张窗口）。
⚠ 这条现在是**实测事实**，不是"保守假设"：若哪天服务端改成按可注册域计，
本地闸门会变得**过于乐观** —— 第一症状是子域发码开始收到
`Too many registration attempts from this email domain`；那时把
`domain_limiter` 的键改成父域。

（另：Worker 只在自己配置的域名上建地址 —— `probe.own.example` 会 HTTP 400，
所以子域这条路即使成立，也要先改 Worker。本项目的 `sub.own.example` /
`sub.smailr.example` 已经在 `AR_MAIL_DOMAINS` 里，所以可以直接测。）

### ③ 验证码邮件

```text
from:    noreply@agnes-ai.com
subject: Your Agnes Platform Verification Code
```

Worker 侧**已配好规则**，直接给出结构化结果：

```json
{"extracted_json":"[{\"value\":\"763388\",\"type\":\"code\"}]","extracted_type":"code"}
```

正文（`body` / `body_text`）里也能正则抽到同一个 6 位码，作为兜底
（历史行、规则缓存窗口内）。实测到达延迟约 **1.5s**。

### ④ 注册

```http
POST /api/user/register
x-user-language: zh
{"email":"<addr>","password":"<pwd>","password_confirm":"<pwd>","code":"<6位>"}
```

```json
{"code":200,"message":"ok","data":{
  "access_token":"eyJ...","token_type":"bearer",
  "user":{"id":1132827,"uuid":"9d50...","display_id":"2026...","email":"<addr>",
          "is_active":true,"created_at":"...","last_login":null}}}
```

⚠ **注册响应里已经带 `access_token`**，但本链路**仍然**会在下一步再走一次
`/api/user/login` —— 那一步才是"密码真能用"的证明（注册返回的 token 可能是
"刚建号时发的"，与密码无关）。

### ⑤ 密码登录

```http
POST /api/user/login
{"username":"<邮箱>","password":"<pwd>"}      ← 注意字段名是 username，值是邮箱
```

成功返回与注册同形的 `data.access_token`（**另一条** JWT）+ `user`。
失败：`401 {"code":401,"message":"Invalid username or password"}`。

### ⑥ 建 API Key

```http
POST /api/token
Authorization: Bearer <JWT>
{"name":"auto"}
```

```json
{"code":200,"message":"ok","data":{
  "id":1283820,"user_id":1132827,"name":"auto","key_profile":"default","status":1,
  "remain_quota":0,"unlimited_quota":false,
  "key_preview":"sk-XXXXXX…XXXXXX","key":"sk-<48 chars，明文，只在创建时返回一次>"}}
```

- `data.key` 是**明文 key，只在创建时返回一次**（列表接口只给 `key_preview`）。
  台账丢了 = key 丢了。
- `GET /api/token`（列表）还带配额：
  `default_quota:{max_keys:20,current:1,keys_remaining:19}`。

### 只读校验

| 端点 | 用途 | 实测 |
| --- | --- | --- |
| `GET /api/user/self` | 账号资料 | 200，`data.is_active=true` |
| `GET /api/user/balance` | 余额 | 200，`available_total_minor=0.0`（免费档） |
| `GET /api/user/subscription` | 订阅 | 200，`data:null`（= 免费档） |
| `GET /api/token` | key 列表 + 配额 | 200 |

### 推理网关（`sk-` key 的地盘）

```http
GET  https://apihub.agnes-ai.com/v1/models           Authorization: Bearer sk-...
POST https://apihub.agnes-ai.com/v1/chat/completions {"model":"agnes-2.5-flash", ...}
```

实测 `GET /v1/models` 返回 11 个模型（`agnes-2.5-flash` / `agnes-3.0-flash` /
`agnes-2.5-pro` / `agnes-image-*` / `agnes-video-*` …），
`POST /v1/chat/completions` 用 `max_tokens=8` 返回 200 + `usage`。

⚠ **控制台 JWT 与 `sk-` key 是两套东西**：JWT 打 `platform-backend`，
`sk-` 打 `apihub`。拿错必 401。

---

## 3. 限流：实测与未测

### 已实测（结论确定）

- **注册尝试限流是“按 IP”的**，消息固定为：

  ```text
  HTTP 400 {"code":400,"message":"Too many registration attempts from this IP.
            Please try again later"}
  ```

  🔴 它是 **400，不是 429** —— 只看状态码必然漏判。判据见
  `src/agnes.py:_is_ip_limited`（要求出现 `too many registration attempts`
  这个具体短语）。

- **两个端点共用同一张计数**：`/api/verification` 与 `/api/user/register`
   在被限流时返回**同一条消息**。

  🔴 **推论（很重要，曾被读错）：窗口计的是「请求」，而一个账号要打两枪**
  ⇒ 上限 `MAX` 实际只放行 `MAX/2` 个账号/窗口。2026-10-18 起默认
  `AR_REGISTER_MAX_PER_WINDOW=4` / `AR_DOMAIN_MAX_PER_WINDOW=8`
  （IP 阈值实测 ≈4、域名阈值实测 ≈10，所以都留了余量）。
  想跑 N 个账号，两个维度的上限都要 ≥ `2N`。

- **域名维度在注册时换不动** —— 域名已经写死在邮箱地址里。所以本地闸门
  （`src/pipeline/limiter.py`）必须**一次占够 `REQUESTS_PER_ACCOUNT` 格才发码**：
  "只留 1 格就发码"= 码烧了、注册进不去。

  ⚠ 实测（2026-10-14，5 账号跑批）：**3/5** 是这个形态 ——
  3 条记录 `code='ok(...)'`（轮询 5 / 3 / 5 次）+ `register='failed' [quota_guard]`，
  而 `own.example` 与 `sub.own.example` **各 2/2 打满**。

  🔴 根因不是"阈值太小"，而是**竞态**：`provision` 的"看窗口还有没有位置"
  与"占位置"不是原子的 ⇒ 两路生产者可以**同时**看到还有位置、然后一起发码。
  修后不变量：**一个账号要么两枪都能发，要么一枪不发**（`skipped`）。
  回归用例：`tests/test_pipeline.py::test_concurrent_producers_cannot_burn_codes`
  （用 barrier 把竞态钉成确定性的；在修前它报 `{'failed': 2}`）。

- **`login` 不受影响**：连打 6 次错误密码 → 全是 `401`，没有 429。

  🔴 2026-10-14 **加强复测到 25 次**错密码登录，仍然**全程 401**（每次
  0.6~2.5s，无任何限流）⇒ **`login` 不消耗注册尝试窗口**。

  ⚠ 这与同类项目（`hackerxiaow/agnes-ai-register`）的记载**相反** —— 它写
  "发码/注册/登录/建Key **都计数**"。以**实测**为准：本项目的 IP 窗口只算在
  `发码 + 注册` 两枪上（`provision` / `finish` 各 1 格），`login` **不计**。

  ⚠ **仍未测**：`create_key` 是否计数。要有效 JWT 才能试，而试的过程会
  **真建出 key**（有副作用），所以没测。

- **请求体校验先于限流检查**：密码 < 8、两次密码不一致、`code` < 4 位会返回
   各自的校验错误（`400 Password must be at least 8 characters` /
   `400 Passwords do not match` / `422 body -> code: String should have at least
   4 characters`），**不触发**限流消息。⇒ 不能拿"没被限流"当"请求有效"。

### 窗口长度与阈值（2026-10-18：台账反推 + 受控实测）

🔴 **旧结论 ">45 分钟" 的**依据**不成立**：它来自**连续打点**的探针，每 30s
对同一个出口打一枪。如果**被拒的那一枪也计数**（服务端回了限流，说明它收到了），
那每次探测都在给窗口**续命** —— "一直 400"只能证明"你一直在打它"，
**不能**反推窗口长度。这个陷阱现在写进了
`tools/probes/probe_window_limits.py` 的模块 docstring。

⚠ 但 ">45 分钟" 这个**数字本身被证实了**：2026-10-18 的受控复测在
**45.37 分钟仍被限流**（见下）⇒ 它是**下界**，不是被推翻。所以默认窗口
**保持** 3600s 附近（60 分钟），但只比下界多 ~15 分钟余量；要绝对安全就调到
5400s+（代价是吞吐）。

#### 台账反推：**恢复间隔**，不是窗口长度（零窗口代价）

`tools/ops/analyze_window_recovery.py` 从 2026-10-13~17 的台账里挖出
"某维度在 T 被限流 → 同一维度下一次发码成功在 T+Δ"的样本：

| 维度 | 样本 | 最短恢复间隔 | 说明 |
| --- | --- | --- | --- |
| IP（按出口） | 2 | 14.88 分钟 | 同一出口被 IP 限流 → 14.88 分钟后发码成功 |
| 域名（同出口） | 3 | 14.88 分钟 | 同一域名 + 同一出口的失败 → 成功 |
| 域名（跨出口） | 11（其中 2 条 <60s） | 0.5s / 7.9s | A 出口刚被限流、B 出口立刻成功 ⇒ 不是严格全局计数 |

🔴 **不要把 14.88 分钟读成窗口长度。** 它只说明"到那时计数已低于阈值"
（恢复间隔），而受控复测在 **16.14 分钟仍被域名限流** ⇒ 14.88 分钟**不是**
窗口上界。原因：失败那一枪是否计数、注册请求是否也计数、阈值是多少，
都会改变"多久才降到阈值以下"。

#### 受控实测：域名阈值 ≈10，且**跨出口计数**

2026-10-18，`probe_window_limits.py --mode burst` 在一个**全新域名**
（`own2.example`）上、用 **10 个不同出口**连打（约 5 分钟）：

```text
#1 .. #9   200 {"code":200,"message":"ok"}
#10        400 Too many registration attempts from this email domain
```

⇒ **域名阈值 ≈10 次/窗口**（不是旧写的 2~3），且**不同出口一起计数**
⇒ 域名窗口的计数是**全局**的（换出口解决不了）。

🔴 **两次独立复测**（`own2.example` 与 `zeppost.com`，都是全新域名）
都是"前 9 枪过、第 10 枪域名限流" ⇒ 阈值 ≈10 不是单次巧合。

⚠ 同一天还测了两个反例：

- 同一域名、**不同出口**打 3 枪：全过；
- 同一域名、**同一出口**打 3 枪：也全过。

⇒ 阈值不是 2~3（旧文档把**本地配置**当成了服务端阈值）。

#### 受控实测：IP 阈值 ≈4（旧 "~16" 是域名维度污染出来的假数）

同一天，在**同一个出口**上连打 `smailr.example`：前 4 枪 `200 ok`，**第 5 枪**
`400 Too many registration attempts from this IP` ⇒ **IP 阈值 ≈4 次/窗口**。

🔴 这条推翻旧注释里的 "一次 ~16 连发即触发"：那次突发**固定用一个域名**，
先撞的是域名维度（旧文档又把本地默认 2~3 当成服务端阈值）⇒ "~16" 是
**域名维度的聚合**，不是 IP 阈值。实际 IP 阈值只有 ~4。

⚠ 这也是"子域测试只能用跨出口"的原因：IP 阈值（≈4）< 域名阈值（≈10），
**同一个出口根本打不满域名窗口**。但域名计数是全局的（10 个不同出口一起
计数），所以跨出口的子域测试仍然有效。

⚠ 但"单一 10 次/滑动窗口"也**不成立**：

- 在 `own.example` 上，43 分钟前有 3 枪（01:38），随后 02:18~02:22 又打
  6 枪全过、第 7 枪被域名限流。如果窗口是"10 次/滑动窗口"且阈值 10，
  这只能是那 3 枪**仍在窗口里**（⇒ W ≥ 43 分钟）。
- 可台账里同一域名在 02:17 被限流、02:32（15 分钟后）就发码成功了。

两者不可能同时由**单一滑动窗口**解释 ⇒ 更像**多级限流**（短时突发 + 长时
持续）或**令牌桶**，而不是"10 次 / 固定窗口"。

⚠ 那 2 条 **0.5s / 7.9s 的跨出口成功**也说明它不是严格一致的全局计数器
—— 更像 CF 边缘的**最终一致**限流。所以本地闸门按全局域名分桶仍然是对的
（保守方向），但不能指望它和服务端逐秒对齐。

#### 受控实测：窗口恢复（单次触碰）

`--mode recovery` 的设计是：打满 → **静默 wait 秒（一枪都不打）** → 只探一枪。
2026-10-18 在 `own2.example` 上打满后静默 15 分钟：

```text
01:59:34  打满（第 1 枪就确认域名限流）
02:15:42  探测：仍是 400 Too many registration attempts from this email domain
          ⇒ 距打满 16.14 分钟，**W > 16.14 分钟**
```

⇒ **窗口长度 > 16.14 分钟**（下界）。"≤14.88 分钟" 那个台账数字是恢复间隔，
不是窗口长度。

**45 分钟复测**（`zeppost.com`，02:31:08 打满 → 03:16:30 探测）：

```text
03:16:30  400 Too many registration attempts from this email domain
          ⇒ 距打满 45.37 分钟，**W > 45.37 分钟**
```

⇒ **窗口长度 > 45.37 分钟**。旧的 ">45 分钟" 因此**被证实为下界**；
上界仍然未知（可能是 1 小时 / 2 小时 / 按天）。

#### 仍然未测准（**不要当结论用**）

- **窗口长度的上界**：只知道 **>45.37 分钟**（两次受控复测：16.14 分钟、
  45.37 分钟都仍限流）。要收窄就换域名、用更大的 `--wait` 再跑（每次
  **只探一枪**）；在测出上界之前，`AR_*_WINDOW_S=3600` 只有 ~15 分钟余量，
  要绝对安全就调到 5400s+。
- **是不是多级限流**：`own.example` 的"3 枪 43 分钟后仍算数"与台账的
  "15 分钟就恢复"互相矛盾 ⇒ 很可能是**短时突发 + 长时持续**两级，但具体
  分界与容量都没测。
- **IP 阈值**：已测到 **≈4**（同一出口 4 枪过、第 5 枪 IP 限流），但只有
  一次样本；是否也是多级 / 是否按 IP+域名 分桶仍未测。
- **是否按 IP+UA / IP+指纹**：只用过一个 UA，没做对照实验。
- **成功注册是否也占用额外计数**：未测。

**怎么复现**：

```bash
python tools/ops/analyze_window_recovery.py            # 零成本，先看台账恢复间隔
python tools/probes/probe_window_limits.py --mode plan # 零请求，看成本
python tools/probes/probe_window_limits.py --mode burst --domain <新域名> --fill 12
python tools/probes/probe_window_limits.py --mode recovery --domain <新域名> --wait 2700
python tools/probes/probe_window_limits.py --mode subdomain --parent <父域> --child <子域> --fill 12
```

> 🔴 **窗口长度这条事实的唯一真源就是本节。** `common/config/` 与
> `README.md` 只**引用**它、不另写数字 —— 同一份知识写两处必然漂移
> （本项目实际漂移过：一处 `>4 分钟`、一处 `>45 分钟`、一处默认 `600s`）。

---

## 4. 出口（代理池）：**IP 维度**那一半的吞吐杠杆

🔴 标题曾写作"唯一的吞吐杠杆"—— **错的**。服务端有**两道**窗口（§3）：
   出口解决 **IP 维度**，`AR_MAIL_DOMAINS` 解决**域名维度**，两者缺一不可。
   只加出口不加域名，会立刻撞域名窗口（反之亦然）。

注册尝试限流按 IP ⇒ **每个出口各自有一张窗口**。

```text
--producers 4                 → 4 个账号在同一出口上各发一次码
                                ⇒ 撞同一张窗口，第 N 个起 400
--producers 4 + 5 个出口       → 每个出口一张窗口 ⇒ 吞吐 ≈ ×5
```

所以本地闸门（`pipeline.register_limiter`）**按出口分桶**，而不是"每进程全局
最小间隔"—— 后者两头都不对：既挡不住单出口超限，又白白把多出口串行化。

🔴 窗口事件**跨进程持久化**（`src/windowstore.py` → `state/register_windows.jsonl`）。
在此之前它们是进程内的：`python run.py --count 1` 连跑两次，第二个进程**仍然
认为窗口是空的** ⇒ 闸门形同不存在（台账实测 17 条 `quota` 失败就是这个后果）。
所以**不要**把 `state/` 当临时目录清掉。

**撞到限流时的第一步是换出口，不是原地等**（`_rotate_slot`）。换不动
（没配池子 / 只有 1 个出口 / 别的都占着）才退避重试，且退避**封顶 45s** ——
窗口长度未测准（受控实测 **>45.37 分钟**；旧 ">45 分钟" 是下界），
按账号等满一个窗口会把批量拖死。

### 出口健康：看**失败率 + 样本量**，不要看失败数

⚠ **本节写错过两次**，都留在这里当反面教材 —— 因为错的方式很典型：

**第一版**："34 条 `network` 失败在出口上**均匀分布**（5~12 条/出口）
⇒ 链路侧现象，不是某个代理坏了"。**两处错**：

1. **看的是失败数，不是失败率。** 同样多的失败数，完全可以是"某出口用得少、
   每次都失败"与"某出口用得多、偶尔失败"——**数一样，含义相反**。
   判"坏槽位"必须除以该出口的**尝试次数**。
2. **"10 个出口"是错的** —— 那 100 条记录其实只落在 **5 个**出口上。

按**失败率**重算那 100 条：

| 出口 | 尝试 | network 失败 | 失败率 |
| --- | --- | --- | --- |
| `Ssyfbs` | 23 | 12 | 52% |
| `2gPFyK` | 24 | 12 | 50% |
| `GKYDef` | 17 | 8 | 47% |
| `n3bvvr` | 25 | 9 | 36% |
| `TUwmbM` | 17 | 6 | 35% |

⇒ 那**一个时段**里整池子都在 35~52% ⇒ 那阵子是**链路 / 平台侧**的坏时段。

**第二版**：改用直连实测（每出口 N 次 `GET /api/user/self`，无鉴权 ⇒ 401，
**不消耗注册窗口**），结果**两次互相矛盾**：

```text
第 1 次（10:5x，每出口 5 次）
  9 个出口   5/5 成功
  1 个出口   0/5 全 SSLError   2gPFyK    ← 看上去"持续坏掉的单个出口"
  直连基线   5/5 成功

第 2 次（几分钟后，每出口 15 次，含直连共 11 个出口）
  全部 15/15 成功，共 165/165，失败率 0%   ← **包括 2gPFyK**
```

🔴 ⇒ **同一小时内"0/5 全坏"与"15/15 全好"可以同时是真的**。
失败是**时间聚集**的（几秒~几分钟的坏时段，影响当时**所有**出口），
**不是按槽位稳定分布的**。

🔴 **所以正确判据是三条，缺一不可**：

1. **看失败率，不是失败数**（见上）；
2. **样本量要够** —— 5 次 `0/5` **不足以**判"坏槽位"。本项目真的因此差点把
   一个**好**槽位从 `.slots.txt` 里删掉 —— 那会永久砍掉 1/10 的吞吐，
   而它几分钟后就是 `15/15`；
3. **横向 + 时间对照** —— 同一时刻全体一起坏 ⇒ 链路 / 平台侧的坏时段；
   只有**在多个时间点都**显著更差，才谈得上"坏槽位"。

⇒ 结论：**当前没有持续坏掉的出口**（165/165）。撞到 `network` 的正确反应是
**换出口重试**（换个出口往往就跳出了那个坏时段），而**不是**把出口拉黑。
（另：10 个槽位确实是 10 个**不同**的出口 IP，不是共用一张窗口 —— 实测过。）

形态只有两种（都是"请求可能已落地"，**不是**"连不上"）：

```text
RemoteDisconnected('Remote end closed connection without response')
        ← 请求已写出、服务端没回响应就断连
SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING] ...')
        ← TLS 流上读到 EOF（**握手之后**，不是握手失败）
```

⚠ 这里也差点归错：`SSLError` 望文生义会当成"TLS 握手失败（请求没出去）"，
而实测原文是 `SSLEOFError`。**看异常原文，别看类名。**
⇒ 两者都**不能**当作“可以安全重试”的依据（重试可能变成第二封验证码 +
第二格服务端窗口）。

⛔ 这两种形态归 `network`（可重试的那一类），**不要**归成 `rejected`
（确定性拒绝，修法是改输入）或 `quota`（限流，修法是换出口 / 等窗口）——
三类修法完全不同，归错会把排查引到相反方向。

🔴 **收信侧不走代理**（`AR_PROXY_MAIL` 默认 0）⇒ `mailbox` 阶段的
`ConnectionResetError` 是 **CF Worker 直连**的问题，与出口池**无关**。
判"是不是代理池的锅"时先把这类排掉。

⚠ 本机（`203.0.113.10`）是**机房 IP**。若观察到限流比预期严得多，
先怀疑 IP 信誉，再怀疑阈值。槽位实例可复用 Intern-Register-Tool 的
`tools/ops/proxypool_ctl.py start`，体检用 `tools/probes/probe_slots.py`。

---

## 5. 邮箱侧：CF Worker 的实测与坑

- **收信一律走 `/api/inbox?email=`**，不要用 `/admin/all`：
  后者不支持按收件人过滤（每次读 N+1 行，烧 D1 读配额），而且返回的是
  "最新 N 条"——D1 retention 只有 100 行，这张表被别的项目灌满时，
  我们的邮件会被**挤出窗口，永远读不到**。按收件人索引查则挤不掉。
- **5xx 必须重试而不是判死**：Worker 会间歇性抛 Cloudflare `Error 1101`，
  而那时邮件好好躺在库里。轮询本来就是在等，多等几次的代价远小于丢掉一个
  **已经注册成功**的账号。4xx（401 凭据错 / 404）才是我们的问题。
- **`since_ts` 必须传**（毫秒）：补跑 / 重试时收件箱里可能还躺着上一轮的
  验证码，不设下限就会拿到一个**已经用过**的码，然后服务端报"验证码错误"，
  而真正的原因是我们在用旧信。
- `received_at` 是**毫秒**（`1791856228371`），与 `receivedDateTime` 的 ISO 串
  同一时刻。把秒当毫秒传进 `since_ts`，过滤会**全部失效**。

---

## 6. 判据表（读代码时对着看）

| 现象 | 分类 | 修法 |
| --- | --- | --- |
| `400 Too many registration attempts from this IP` | `quota` | 换出口 / 等窗口 |
| `429`（任意端点） | `quota` | 同上 |
| `400 This email is already registered` | `rejected` | 换邮箱（地址被回收复用） |
| `400 Password must be at least 8 characters` | `rejected` | 改 `AR_PASSWORD_LEN` |
| `400 Passwords do not match` | `rejected` | 内部 bug（两次密码不一致） |
| `401 Invalid username or password` | `rejected` | 密码没生效 / 台账密码错 |
| `5xx` / 超时 | `network` | **只有幂等只读**调用自动重试一次；写端点见下 |
| 邮箱 Worker 读不出来 | `mail` | 看 Worker / D1，不是"邮件没到" |
| 等码超时 | `rejected`（`MAIL_TIMEOUT`） | 确认 Worker 规则 / 域名 |
| 网关 `401/403`（`/v1/models`） | `verify` | key 无效（非致命，账号仍算成功） |

### `send_code` 撞 `network` 时**不重试** —— 这是查证过的决定

`network` 里**只有幂等只读**调用（`user_self` / `balance` / `list_keys`）会自动
重试一次。`send_code` / `register` / `login` / `create_key` **一律不重试**。

2026-10-14 查了台账里 34 条 `network@code` 的**异常原文**：

| 形态 | 条数 | 请求落地了吗 |
| --- | --- | --- |
| `RemoteDisconnected('Remote end closed connection without response')` | 25 | **可能已落地**（请求已写出） |
| `SSLEOFError('[SSL: UNEXPECTED_EOF_WHILE_READING]')` | 9 | **可能已落地**（握手**之后**读到 EOF） |

⇒ **34 条里 0 条属于"确定没落地"** ⇒ "只对没落地的重试"这条窄路
**救不回任何一条**。而重试一条可能已落地的 `send_code` 会变成
**第二封验证码 + 第二格服务端窗口**：代价确定，收益不确定。

⚠ 差点归错：`SSLError` 望文生义会当成"TLS 握手失败（请求没出去）"，
而实测原文是 `SSLEOFError`。**看异常原文，别看类名。**

🔴 **不重试的实际代价比预想小**：2026-10-14 11:36 跑批里 3 条 `send_code`
撞网络错误，因为请求**没完成**，所以**零验证码被烧**、零服务端窗口被白占 ——
损失只是一个账号。（"码烧了账号还没了"是另一个问题，由 P6 的预留修复解决。）
