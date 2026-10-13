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
* **没有域名门**：`@own.example`（本项目 CF Worker 的自有域名）直接过，
  这是它比 OpenXLab（`A0232` 域名门）省事的地方 —— 所以本项目**只有 Worker
  一个邮箱源**，不引入 Intern 那套可插拔抽象。

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

## 4. 出口（代理池）为什么是唯一的吞吐杠杆

注册尝试限流按 IP ⇒ **每个出口各自有一张窗口**。

```text
--producers 4                 → 4 个账号在同一出口上各发一次码
                                ⇒ 撞同一张窗口，第 N 个起 400
--producers 4 + 5 个出口       → 每个出口一张窗口 ⇒ 吞吐 ≈ ×5
```

所以本地闸门（`pipeline.register_limiter`）**按出口分桶**，而不是"每进程全局
最小间隔"—— 后者两头都不对：既挡不住单出口超限，又白白把多出口串行化。

**撞到限流时的第一步是换出口，不是原地等**（`_rotate_slot`）。换不动
（没配池子 / 只有 1 个出口 / 别的都占着）才退避重试，且退避**封顶 45s** ——
窗口长度未知，按账号等满一个窗口会把批量拖死。

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
| `5xx` / 超时 | `network` | 可重试（幂等请求自动重试一次） |
| 邮箱 Worker 读不出来 | `mail` | 看 Worker / D1，不是"邮件没到" |
| 等码超时 | `rejected`（`MAIL_TIMEOUT`） | 确认 Worker 规则 / 域名 |
| 网关 `401/403`（`/v1/models`） | `verify` | key 无效（非致命，账号仍算成功） |
