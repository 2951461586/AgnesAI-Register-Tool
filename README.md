# AgnesAI-Register-Tool

Agnes AI（国际站）的**端到端注册全链路**：CF Worker 域名邮箱 → 邮箱验证码 →
注册 → 密码登录 → 建 API Key → **推理网关实测 key 可用**。批量可跑，
带台账、按出口的限流闸门、代理槽位池与收尾报告。

```bash
cp .env.example .env      # 填 AR_WORKER_* 三项（见下）
python run.py             # 跑 1 个账号
python run.py --count 6   # 跑 6 个
```

**零浏览器**：整条链路是标准 JSON API，没有验证码图片、没有 WAF 解盾，
运行依赖只有 `requests` 一个（对比 [Intern-Register-Tool] 必须用真浏览器
解阿里云验证码）。协议与实测证据在 [`docs/protocol.md`](docs/protocol.md)。

本项目是 [Intern-Register-Tool] 架构的**第三个同族实现**（继
[Hark-Register-Tool] 之后），目录与分层刻意保持一致，便于在三者之间迁移经验：
`common/config.py` + `src/{tempmail,agnes,apikey,pipeline,ledger,quota,report,redact,fsutil,cli}.py` +
`run.py` + `docs/` + `tests/` + `tools/`。

---

## 怎么跑

一律**从项目根**执行：

```bash
python run.py                          # 1 个账号
python run.py --count 6                # 6 个（默认 4 路生产者 × 4 路消费者）
python run.py --count 6 --producers 1 --workers 1   # 强制顺序（最保守）
python run.py --out accounts.json      # 导出到指定文件（**不落台账快照**）
python run.py --ignore-quota           # 跳过本地配额闸门
python run.py --quiet                  # 只输出汇总
```

启动阶段会校验配置：缺 `AR_WORKER_*` 直接退出（码 4），
本地配额窗口已满直接退出（码 2，**一个请求都不发**）。

### 配置（`.env`，已在 `.gitignore` 中）

| 变量 | 必需 | 说明 |
| --- | --- | --- |
| `AR_WORKER_BASE` | ✅ | CF Worker 地址，如 `https://<worker>.<sub>.workers.dev` |
| `AR_WORKER_ADMIN_TOKEN` | ✅ | Worker 的 Admin Token |
| `AR_WORKER_DOMAIN` | ✅ | 收信域名（实测**自有域名不需要报备**，但平台侧有**域名白名单** —— 见 `docs/protocol.md` §2） |
| `AR_MAIL_DOMAINS` | — | 收信**域名池**（逗号分隔，轮换用）。留空退回 `AR_WORKER_DOMAIN`。**第二个吞吐杠杆**（见下） |
| `AR_PASSWORD_LEN` | — | 默认 16。服务端硬要求 ≥ 8 |
| `AR_KEY_NAME` | — | 创建的 API Key 名称，默认 `auto` |
| `AR_VERIFY_MODEL` / `AR_VERIFY_CHAT` | — | key 校验用的模型 / 是否真打一次 chat |
| `AR_REGISTER_MAX_PER_WINDOW` / `AR_REGISTER_WINDOW_S` | — | **按出口**的注册尝试限速（默认 **2 次** = **1 个账号** / **3600s**，**保守值**，见下）。⚠ 计的是**请求**，不是一个账号一枪 |
| `AR_DOMAIN_MAX_PER_WINDOW` / `AR_DOMAIN_WINDOW_S` | — | **按收信域名**的注册尝试限速（默认 **2 次** = **1 个账号** / 3600s）。与 IP 维度**分开配** |
| `AR_LIMITER_MAX_WAIT_S` | — | 本地闸门「等不起就换维度」阈值（秒，默认 20） |
| `AR_REG_QUOTA_MAX` / `AR_REG_QUOTA_WINDOW_H` | — | **爆炸半径上限**（默认 30 / 24h），不是服务端配额 |
| `AR_PROXY` / `AR_PROXY_SLOTS(_FILE)` / `AR_PROXY_SLOT_TIMEOUT` | — | 出口代理与槽位池（多出口并发） |

完整清单与每项的**取值理由**见 [`.env.example`](.env.example)（注释就是设计文档）。

---

## 三件事必须先知道

### 1. 限流有**两个维度**（IP + 域名），两个杠杆缺一不可

Agnes 对**注册尝试**（`/api/verification` 与 `/api/user/register`，**共用一张
计数**）有**两道**限流 —— 一道按 **IP**，一道按**收信域名**：

```text
400 {"code":400,"message":"Too many registration attempts from this IP. Please try again later"}
400 {"code":400,"message":"Too many registration attempts from this email domain. Please try again later"}
```

🔴 它们是 **400，不是 429**。所以：

* 🔴 **窗口计的是「请求」，而一个账号要打两枪**（发码 + 注册共用同一张计数）
  ⇒ 默认的 `2` 实际只放行 **1 个账号 / 窗口**，不是 2 个。想跑 N 个账号，
  两个维度的上限都要 ≥ `2N`。这一点曾被读成"2 个账号"，直接导致"码烧了、
  注册进不去"（见下面第 4 点）；
* **只调大 `--producers` 不会提高吞吐** —— 那是同一张窗口里的名额；
* 提高吞吐要**两个杠杆一起上**：`AR_PROXY_SLOTS`（N 个出口 = N 张 IP 窗口）
  **和** `AR_MAIL_DOMAINS`（M 个域名 = M 张域名窗口）。只加一个，会立刻撞上
  另一个维度的窗口；
* 撞到限流时本工具**先换维度**（IP 撞了就换出口、域名撞了就换域名），
  换不动才退避重试。

⚠ **窗口长度尚未测准** —— 这条事实的唯一真源是
[`docs/protocol.md`](docs/protocol.md) §3「限流：实测与未测」，
**本文件刻意不重复那个数字**（同一份知识写两处必然漂移 —— 本项目实际漂移过：
一处 `>4 分钟`、一处 `>45 分钟`、一处默认 `600s`）。所以本地闸门取保守值
（默认 `2 次 / 3600s`，= 1 个账号）。测准它的方法见
`tools/probes/probe_register_limit.py`。

🔴 **域名维度换不动，所以本地必须一次占够两枪**（`src/pipeline.py` 的
`REQUESTS_PER_ACCOUNT`）。注册时域名已经定死在邮箱里 —— 只留一格就发码，
等于"码烧了、注册却进不去"。所以 `provision` 会先确认域名窗口装得下
**两枪**，装不下就**一枪都不发**（`skipped`），而不是先烧了再说。
实测依据：一次 5 账号跑批里 3 个就是这个形态（两个域名各 2/2 打满）。

### 2. 台账是账号的**唯一副本**

邮箱是临时邮箱（收不到找回信），密码与 `sk-` key **只存在于台账里**，
而 `data.key` 只在创建时返回一次。所以：

* `ledger/` 整个目录在 `.gitignore` 中；
* **不要**把它当临时文件清掉 —— 定期另存一份到别处；
* 每次跑批按 email **合并**（不是覆盖），并有**防静默缩水护栏**：
  若一次小规模运行会让台账条数变少，它**拒绝写盘**并退出码 3。

### 3. `verify` 阶段才证明 key 真能用

控制台返回了 `sk-...` 只说明"建出来了"。本工具会在流水线末尾**并行**打一次
推理网关（`GET /v1/models` + `POST /v1/chat/completions`），结果写进
`stages["verify"]`。它是**非致命**的：网关侧失败不会把账号标成 failed，
但你在报告里能一眼看到（避免"看着成功、其实不能用"的假绿账号）。

---

## 输出与台账

默认（不给 `--out`）落在台账目录：

```text
ledger/runs/<日期>/accounts-<时间戳>.json   ← 合并后的**全量**快照（= 读源）
ledger/latest.json                          ← **本批结果**（含失败 / 跳过）
```

🔴 读源是**最新的全量快照**，**不是** `latest.json`（后者只含本批，拿它当读源
会让台账停止累积）。详见 [`src/ledger.py`](src/ledger.py) 的模块 docstring。

每条记录包含：`email` / `password` / `user_id` / `user_uuid` / `jwt` /
`api_key` / `key_id` / `key_preview` / `plan` / `balance_minor` / `key_max` /
`models_count` / `status` / `error_kind` / `stages` / `timings` / `proxy`。

`status` 有三态：`success` / `failed` / `skipped`（**跳过 = 一个请求都没发**，
与"试过了不行"是两件事）。

---

## 目录

```text
common/config.py     全局配置与启动校验（AR_* 环境变量）
src/tempmail.py      CF Worker 邮箱客户端（建地址 + 按收件人收信 + 抽验证码）
src/agnes.py         Agnes 控制台协议（verification/register/login/token/...）
src/apikey.py        推理网关侧的 key 可用性校验
src/pipeline/        两段式流水线（P5 拆包：每个子模块只讲一件事）
  ├ limiter.py       本地闸门：滑动窗口 / 域名预留 / 限流器工厂 / 换出口
  ├ record.py        AccountRecord / Ticket / classify / STAGES
  ├ provision.py     生产者段：建邮箱 → 发码 → 等码
  ├ finish.py        消费者段：注册 → 登录 → 建 Key → 只读校验
  └ batch.py         编排：生产者池 ‖ 消费者池 + 配额闸门 + 收尾
src/ledger.py        账号台账（合并 + 防静默缩水）
src/quota.py         本地配额闸门（爆炸半径上限）
src/jsonlstore.py    JSONL 状态存储（唯一实现：追加 / 宽容读取 / 原子重写）
src/windowstore.py   滑动窗口事件（**跳进程持久化**）
src/proxypool.py     槽位代理池
src/report.py        收尾报告（纯展示，依赖注入）
src/{redact,fsutil,cli}.py  脱敏 / 原子写盘 / CLI 共用骨架
run.py               CLI 入口
docs/protocol.md     协议与实测（**改代码前读它**）
tests/               离线用例（零网络）
tools/               探针与运维脚本
```

## 常用工具

```bash
python tools/probes/probe_domains.py         # 哪些收信域名真能收到 Agnes 的信
python tools/probes/probe_register_limit.py  # 注册尝试限流的边界（⚠ 会消耗窗口）
python tools/probes/probe_slots.py           # 出口池：IP 是否两两不同 / Agnes 是否可达
python tools/ops/verify_accounts.py          # 台账账号存活复核（只读）
python tools/data/export_keys.py             # 导出 sk- key 列表
```

---

## 质量闸门

```bash
python -m pytest              # 全部**离线**用例（零网络，不需要任何凭据）
python -m ruff check .        # lint
python -m ruff format --check .
```

⚠ 这里刻意**不写用例数** —— 写死了就会漂移（而“文档里的数字跟不上实现”
正是 P3 要防的事）。

CI（`.github/workflows/ci.yml`）跑上面三样 + `gitleaks` 密钥扫描（**全历史**）。

🔴 用例刻意**不依赖宿主的 `.env`**：清掉 `AR_WORKER_*`（CI 就是这样）也全绿
—— `tests/conftest.py` 的夹具把域名池 / 槽位池 / 状态文件都替换成确定值。
这条曾经**不成立**（success 路径会以"没有可用的收信域名"失败），
而那正是本项目长期上不了 CI 的原因。

---

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 至少有一个账号成功（或没有要跑的） |
| 1 | 本批**全部失败** |
| 2 | 本地配额闸门拦下（**未发出任何请求**） |
| 3 | 台账防静默缩水护栏触发（拒绝写盘） |
| 4 | 配置有问题（缺凭据 / 值不合法，如窗口上限装不下一个账号） |
| 5 | 内部错误（记录序列化失败） |

---

## 已知限制

* **限流窗口未测准** —— 默认值是保守估计，不是服务端阈值（实测下界见
  `docs/protocol.md` §3，那是唯一真源）。
* **单出口 / 单域名吞吐有限**：两个杠杆（`AR_PROXY_SLOTS` + `AR_MAIL_DOMAINS`）
  不一起配时，批量会被同一张 IP 或域名窗口卡住。
* **邮箱源只有 CF Worker 一个** —— 自有域名不需要报备（没有 OpenXLab 那种
  `A0232` 域名门），但**平台侧有一道域名白名单**（可用/被拒的清单见
  `docs/protocol.md` §2）；要换源得改 `src/tempmail.py`。
* **不处理找回密码 / 二次验证** —— 临时邮箱收不到找回信，台账丢了就是丢了。
* 本工具仅供学习与自用；请遵守 Agnes 的服务条款。

[Intern-Register-Tool]: ../Intern-Register-Tool
[Hark-Register-Tool]: ../Hark-Register-Tool
