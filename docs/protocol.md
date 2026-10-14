# Agnes AI 注册链路：协议与实测

> 本文件是**现行文档**：动注册 / 登录 / 建 Key / 限流相关代码之前先读它。
> 所有结论都来自**线上实测**（2026-10-13，单一出口 `203.0.113.10`），
> 不是从混淆 JS 里猜的。凡是**没测到**的，都会明确写"未测"——
> 把猜测写成结论是这个项目最不能犯的错。

---

## 1. 全链路（6 步，零浏览器）

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

**没有验证码图片、没有 WAF 解盾、没有浏览器**。整条链路的运行依赖只有
`requests`（对比 Intern-Register-Tool 必须用真浏览器解阿里云验证码）。

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

* `purpose=register` 是实测用的取值（注册流程）。
* **不存在的域名也返回 200**（拿 `@example.com` 试过）—— 服务端不在这一步
  做域名校验，所以这一枪"成功"不代表能收到信。
* ⚠ **"没有域名门"是错的说法**（本节曾这么写）。准确说法是：**没有"自有域名"
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

  🔴 **域名数是吞吐的硬上限**（域名维度 2 格 = 1 个账号）⇒ 可用域名数直接决定
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
| **第三方临时邮箱服务** | ⚠️ 只有**没被列入名单**的能用。实测 `catchmail.io`（3 域名）可用；`1secmail` / `mailinator` / `dropmail` 全被拒；`outlook.com` 系（接码平台）被拒 |

🔴 **第三方源的关键代价**：它的域名是**公共**的 —— 任何人都能在上面建地址
⇒ 那个域名的服务端窗口**与陌生人共享**，可能被别人的量打满且**不可控**。
所以它的定位是**补充窗口**，不是替代自有域名。
（同类项目就记过 `Mail.tm` 的公共域名 `uberip.com`"周期性被封"。）

⇒ 本项目因此支持**多源**（`AR_MAIL_SOURCES`，默认只有 `worker`）：
自有域名当主力，`catchmail` 当溢出容量。详见 `src/mailbox.py`。

⚠ **未测清**：**子域是不是独立窗口**。间接证据是"`own.example` 与
`sub.own.example` 同时成功"—— 若服务端把它们算**同一个域名**，则合计 4 次
请求会超阈值（2~3）⇒ 很可能按**精确 hostname** 计。但没测准，**不当结论用**。
（另：Worker 只在自己配置的域名上建地址 —— `probe.own.example` 会 HTTP 400，
所以子域这条路即使成立，也要先改 Worker。）

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

* `data.key` 是**明文 key，只在创建时返回一次**（列表接口只给 `key_preview`）。
  台账丢了 = key 丢了。
* `GET /api/token`（列表）还带配额：
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

- **注册尝试限流是"按 IP"的**，消息固定为：
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
  ⇒ `AR_*_MAX_PER_WINDOW=2` 的真实含义是 **1 个账号/窗口**，不是 2 个。
  想跑 N 个账号，两个维度的上限都要 ≥ `2N`。

- **域名维度在注册时换不动** —— 域名已经写死在邮箱地址里。所以本地闸门
  （`src/pipeline.py`）必须**一次占够 `REQUESTS_PER_ACCOUNT` 格才发码**：
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

- **请求体校验先于限流检查**：密码 < 8、两次密码不一致、`code` < 4 位会返回
   各自的校验错误（`400 Password must be at least 8 characters` /
   `400 Passwords do not match` / `422 body -> code: String should have at least
   4 characters`），**不触发**限流消息。⇒ 不能拿"没被限流"当"请求有效"。

- **一次 ~16 连发的突发会触发**，且触发后 **>45 分钟仍被拦**：早期 30s 间隔
  探测在 **t+214s** 仍 400；后续同出口复测在 **t+45 分钟**仍被拦
  （⇒ 早期那条「窗口 >4 分钟」的结论已被这个更强的观测取代）。

### 未测（**不要当结论用**）

* **窗口长度**：只知道 **>45 分钟**。可能是 1 小时 / 按天。
  这也是 `.env` 里 `AR_REGISTER_WINDOW_S` 默认取保守值（**3600s**）的原因。

> 🔴 **窗口长度这条事实的唯一真源就是本节。** `common/config.py` 与
> `README.md` 只**引用**它、不另写数字 —— 同一份知识写两处必然漂移
> （本项目实际漂移过：一处 `>4 分钟`、一处 `>45 分钟`、一处默认 `600s`）。
* **阈值 N**：不知道是"N 次/窗口"还是令牌桶（我们的探测本身可能**续命**窗口，
  所以"探测期间一直 400"不能反推窗口长度）。
* **是否按 IP+UA / IP+指纹**：只用过一个 UA，没做对照实验。
* **成功注册是否也占用额外计数**：观测到"2 次发码 + 1 次成功注册"之后就被拦，
  但也可能是那之前的其它请求累积的。

**要测准它**：用 `tools/probes/probe_register_limit.py`，并且
**在探测之间不要发任何其它注册类请求**（否则会污染结论）。

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
窗口长度未知，按账号等满一个窗口会把批量拖死。

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
⇒ 两者都**不能**当作"可以安全重试"的依据（重试可能变成第二封验证码
+ 第二格服务端窗口）。

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

* **收信一律走 `/api/inbox?email=`**，不要用 `/admin/all`：
  后者不支持按收件人过滤（每次读 N+1 行，烧 D1 读配额），而且返回的是
  "最新 N 条"——D1 retention 只有 100 行，这张表被别的项目灌满时，
  我们的邮件会被**挤出窗口，永远读不到**。按收件人索引查则挤不掉。
* **5xx 必须重试而不是判死**：Worker 会间歇性抛 Cloudflare `Error 1101`，
  而那时邮件好好躺在库里。轮询本来就是在等，多等几次的代价远小于丢掉一个
  **已经注册成功**的账号。4xx（401 凭据错 / 404）才是我们的问题。
* **`since_ts` 必须传**（毫秒）：补跑 / 重试时收件箱里可能还躺着上一轮的
  验证码，不设下限就会拿到一个**已经用过**的码，然后服务端报"验证码错误"，
  而真正的原因是我们在用旧信。
* `received_at` 是**毫秒**（`1791856228371`），与 `receivedDateTime` 的 ISO 串
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
