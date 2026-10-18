"""工具实现包。

模块分工（与 Intern-Register-Tool / Hark-Register-Tool 对齐，按 Agnes 的实际形状裁剪）：

    tempmail.py   CF Worker 临时邮箱客户端（建地址 + 按收件人收信 + 抽验证码）
    agnes.py      Agnes 控制台协议客户端（verification / register / login /
                  token / user-self / balance / subscription）
    apikey.py     推理网关侧的 API Key 可用性校验（/v1/models + /v1/chat/completions）
    pipeline.py   两段式流水线（生产者：建邮箱→发验证码→等码；
                  消费者：注册→登录→建 Key→只读校验；末尾并行校验 Key）
    proxypool.py  槽位代理池（多出口 = 多张 IP 窗口；**最少使用优先**）
    ledger.py     账号台账（合并而不是覆盖；防静默缩水护栏）
    quota.py      本地注册配额保护（爆炸半径上限，不是服务端权威计量）
    mailbox.py    邮箱源注册表（**按域名派发**到对应的客户端）
    catchmail.py  catchmail.io 邮箱源（第二个源；公共域名 = **共享窗口**）
    smailr.py     smailr.com 邮箱源（第三个源；4 个域名 + **成功落库后删邮箱**）
    jsonlstore.py JSONL 状态存储（**唯一实现**：追加 / 宽容读取 / 原子重写）
    windowstore.py 滑动窗口事件存储（**跨进程持久化**，与服务端两道窗口同构）
    report.py     批量收尾报告（纯展示，依赖注入，不 import 上层）
    preflight.py  跑批前的链路健康预检（只读，不耗注册窗口）
    captcha.py    TCaptcha 点选 provider（headless Playwright；默认开）
    captcha_solver.py 点选 solver（多尺度/旋转 NCC + 匈牙利分配；可选依赖）
    redact.py     日志 / 输出边界的脱敏
    fsutil.py     原子写盘（唯一实现）
    cli.py        两个 CLI 入口共用的骨架
"""
