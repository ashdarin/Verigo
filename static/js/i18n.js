const VerigoI18n = (() => {
  const english = {
    "单个验证": "Single check", "批量验证": "Bulk check", "查找工作邮箱": "Find work email",
    "面向销售、招聘与出海团队": "For sales, recruiting, and global teams", "先验证名单，再找到能联系上的企业邮箱。": "Verify your list, then find company emails you can contact.",
    "Verigo 将邮箱验证、批量清洗和按域名联系人发现放在一条工作流中，帮助你减少退信、保护发信信誉，并更快触达目标企业。": "Verigo brings email verification, list cleaning, and company email finding into one workflow to reduce bounces and reach target companies faster.",
    "免费验证邮箱": "Verify email free", "查看价格与额度": "View pricing", "单个验证每日免费 20 次": "20 free single checks daily", "注册验证后赠 10 次体验额度": "10 trial credits after verification", "支持 CSV、Excel 与 API": "CSV, Excel, and API supported",
    "减少无效触达": "Reduce wasted outreach", "在发信、导入 CRM 或联系候选人前，先识别明显不可用的邮箱。": "Identify clearly unusable email addresses before sending, importing to CRM, or contacting candidates.",
    "直接找到企业邮箱": "Find company emails directly", "输入公司域名，查找用于销售开发、招聘和合作联络的工作邮箱。": "Enter a company domain to find work emails for sales, recruiting, and partnerships.",
    "按量付费，先免费体验": "Pay as you go, start free", "单个邮箱验证免费；批量验证低至 ¥0.50 / 100 次。": "Single checks are free; bulk verification starts at CNY 0.50 per 100 checks.",
    "一条可行动的工作流": "One actionable workflow", "从名单质量到目标企业邮箱": "From list quality to target company emails", "选择你当前最需要完成的动作，几分钟内得到可用于下一步决策的结果。": "Choose the job you need now and get results you can act on within minutes.",
    "验证单个邮箱": "Verify one email", "在回复、邀请或导入 CRM 前，快速判断一个邮箱是否值得继续使用。": "Quickly decide whether an email is worth using before replying, inviting, or importing it to CRM.", "立即验证": "Verify now",
    "批量清洗名单": "Clean lists in bulk", "导入 CSV 或 Excel，识别明显不可用的地址，让营销和销售名单更干净。": "Import CSV or Excel files to identify clearly unusable addresses and keep sales and marketing lists cleaner.", "批量清洗": "Clean list",
    "企业邮箱查找": "Company email finder", "输入目标公司域名，找到适合销售开发、招聘和商务合作的工作邮箱。": "Enter a target company domain to find work emails for sales, recruiting, and business partnerships.", "查找企业邮箱": "Find company emails",
    "为业务动作而设计": "Built for business action", "让下一封邮件、更值得发出去": "Make the next email worth sending", "保护发信信誉": "Protect sender reputation", "在发送前排除明显无效邮箱，减少退信对发件域名和账号信誉的影响。": "Remove clearly invalid emails before sending to reduce bounce damage to your domain and account reputation.",
    "更快启动企业开发": "Start company outreach faster", "不用先拿到姓名或猜邮箱格式；从目标企业域名开始，直接查找可联系的工作邮箱。": "No need to know a name or guess an email pattern. Start with a company domain and find work emails you can contact.",
    "适合团队工作流": "Fits team workflows", "支持 Excel、CSV、批量任务和 API，可在 CRM 导入、活动发送或招聘触达前完成检查。": "Excel, CSV, bulk jobs, and API support help you check addresses before CRM imports, campaigns, or recruiting outreach.",
    "常见问题": "Frequently asked questions", "购买或开始验证前，你需要知道这些": "What to know before buying or verifying", "批量验证怎么收费？": "How is bulk verification priced?", "批量验证按额度计费，当前公开价格为 ¥0.50 / 100 次。单个邮箱验证和企业邮箱查找可免费开始使用。": "Bulk verification uses credits and currently costs CNY 0.50 per 100 checks. Single email verification and company email finding are free to start.",
    "Catch-all 邮箱是什么意思？": "What is a catch-all email domain?", "Catch-all 是企业邮箱域名的一种设置：例如对方公司启用它后，发往任意前缀的邮件都可能被服务器接收或转发。因此服务器的“接受”响应不能证明某个具体员工邮箱真实存在，建议单独处理。": "A catch-all is a company-domain setting where emails sent to any prefix may be accepted or forwarded. An accepted server response therefore cannot prove that a specific employee mailbox exists, so handle these separately.",
    "企业邮箱查找和普通验证有什么区别？": "How is company email finding different from verification?", "普通验证用于确认你已有的邮箱是否可用；企业邮箱查找用于获取目标企业的业务联络邮箱，适合销售开发、招聘和商务合作。": "Standard verification confirms whether an email you already have is usable. Company email finding helps you get business contact emails for a target company, for sales, recruiting, and partnerships.",
    "运营监控": "Operations", "额度管理": "Credit management", "资金与使用": "Wallet",
    "运行中": "Online", "验证邮箱领取体验额度": "Verify email for trial credits", "登录": "Sign in",
    "绑定邮箱": "Link email", "修改密码": "Change password", "删除账户": "Delete account", "退出登录": "Sign out",
    "免费单个验证": "Free single check", "验证单个收件地址": "Verify an email", "邮箱地址": "Email address",
    "手动输入": "Paste list", "文件导入": "Import file", "邮箱列表": "Email list", "导入数据": "Import data",
    "选择文件": "Choose file", "验证速度": "Verification speed", "稳定": "Steady", "标准": "Standard", "快速": "Fast", "极速": "Fastest",
    "免费验证": "Free verification", "最近任务": "Recent jobs", "任务工作台": "Job workspace", "等待任务": "Waiting for a job",
    "未开始": "Not started", "排队中": "Queued", "等待验证": "Waiting", "验证中": "Verifying", "未完成": "Not completed", "已完成": "Completed", "失败": "Failed", "已停止": "Stopped",
    "无限额度": "Unlimited", "开始验证": "Start verification", "工作邮箱查找": "Work email discovery", "企业邮箱查找": "Company email finder",
    "稳定模式": "Steady mode", "标准模式": "Standard mode", "快速模式": "Fast mode", "极速模式": "Fastest mode",
    "已出结果": "Results ready", "可投递": "Deliverable", "不可投递": "Undeliverable", "待确认": "Pending",
    "全部结果": "All results", "继续未完成验证": "Resume verification", "停止验证": "Stop verification", "下载 CSV": "Download CSV",
    "邮箱": "Email", "结果": "Result", "域名类型": "Domain type", "验证方式": "Method", "服务器响应": "Server response",
    "尚无验证结果": "No verification results yet", "等待验证结果": "Waiting for results", "上一页": "Previous", "下一页": "Next",
    "按姓名查找工作邮箱": "Find a work email by name", "找到目标企业的工作邮箱": "Find a work email by name", "查找邮箱": "Find emails", "免费验证候选邮箱": "Verify candidates free",
    "查找结果": "Search results", "等待查找": "Waiting to search", "请输入姓名和公司域名": "Enter a first name, last name, and company domain",
    "候选邮箱": "Candidate emails", "尚无查找结果": "No search results yet", "报告快照": "Report snapshot", "网站运营概览": "Website operations overview",
    "正在加载数据": "Loading data", "最近 14 天": "Last 14 days", "刷新": "Refresh", "独立访客": "Unique visitors", "今日": "Today",
    "互动会话": "Engaged sessions", "验证提交": "Verification submissions", "平均互动时长": "Average engagement", "已互动会话": "Engaged sessions",
    "流量趋势": "Traffic trend", "独立访客与互动会话": "Unique visitors and engaged sessions", "流量质量": "Traffic quality",
    "已识别的访问会话": "Identified visits", "非 Bot": "Non-bot", "互动跳出率": "Engagement bounce rate", "Bot 占比": "Bot share",
    "转化与注册": "Conversion and registration", "今日从访问到产品动作": "Today's visit-to-action funnel", "真实会话": "Human sessions",
    "免费验证提交": "Free checks", "批量验证提交": "Bulk checks", "验证服务表现": "Verification service", "今日创建任务": "Jobs created today",
    "运行正常": "Operational", "任务完成率": "Job completion rate", "平均处理时长": "Average processing time", "可投递率": "Deliverability rate",
    "已处理邮箱": "Emails processed", "用户与活跃度": "Users and activity", "用户规模与今日真实访问": "User base and today's human visits",
    "账户活跃": "Account activity", "注册用户": "Registered users", "累计": "Total", "已验证用户": "Verified users",
    "可使用验证服务": "Can use verification", "今日独立访客": "Today's unique visitors", "去重访问": "Deduplicated visits",
    "今日互动会话": "Today's engaged sessions", "今日新增注册": "New registrations today", "新账户": "New accounts",
    "今日完成验证": "Email confirmations today", "邮箱确认": "Email confirmation", "收入": "Revenue", "仅统计已支付订单": "Paid orders only",
    "人民币": "CNY", "今日实际收入": "Revenue today", "累计实际收入": "Lifetime revenue", "累计已支付订单": "Paid orders",
    "平均客单价": "Average order value", "账户运营": "Account operations", "验证次数管理": "Verification credit management",
    "账户、使用情况与调整记录": "Accounts, usage, and adjustment history", "账户总数": "Accounts", "已注册账户": "Registered accounts",
    "付费验证次数": "Paid verifications", "全部账户": "All accounts", "体验验证次数": "Trial verifications", "有效体验次数": "Active trial credits",
    "累计已验证": "Total verified", "调整操作": "Adjust credits", "授予或扣减验证次数": "Grant or deduct verification credits",
    "操作": "Action", "授予额度": "Grant credits", "扣减额度": "Deduct credits", "用户注册邮箱": "Account email",
    "查询账户": "Find account", "充值或退款金额（元）": "Payment or refund amount (CNY)", "备注": "Note", "确认授予": "Confirm grant",
    "功能使用趋势": "Feature usage", "单个验证、批量验证与工作邮箱查找": "Single checks, bulk checks, and email discovery",
    "按注册时间排序": "Sorted by registration date", "账户中心": "Account center", "累计充值": "Total paid", "实收金额": "Net revenue",
    "付费余额价值": "Paid credit value", "已用于验证": "Used for verification", "可用验证次数": "Available verifications",
    "含体验次数": "Includes trial credits", "验证使用趋势": "Verification usage", "资金流水": "Transaction history",
    "实收、退款和管理员调整": "Payments, refunds, and adjustments", "邮箱验证指南": "Email verification guide", "名单清洗": "List cleaning",
    "API 文档": "API documentation", "隐私与数据说明": "Privacy and data", "使用边界": "Acceptable use",
    "Verigo 账户": "Verigo account", "注册": "Register", "邮箱或旧用户名": "Email or legacy username", "密码": "Password", "忘记密码？": "Forgot password?",
    "旧账号迁移": "Legacy account migration", "新邮箱": "New email", "发送验证码": "Send code", "确认邮箱": "Confirm email",
    "邮箱验证码": "Email verification code", "完成绑定": "Finish linking", "体验额度": "Trial credits", "验证注册邮箱": "Verify your account email",
    "验证后可领取 10 个体验额度，有效期为 7 天。": "Verify your email to receive 10 trial credits, valid for 7 days.",
    "输入邮箱验证码": "Enter email verification code", "六位验证码": "Six-digit code", "完成验证并领取额度": "Verify and claim credits",
    "账户与数据": "Account and data", "我确认永久删除此账户及其数据": "I confirm permanent deletion of this account and its data",
    "永久删除账户": "Delete account permanently", "查看 API 文档": "View API documentation", "Key 名称": "Key name",
    "创建 API Key": "Create API key", "请立即复制并安全保存": "Copy and store this now", "复制": "Copy",
    "关闭此窗口后，完整 Key 不会再次显示。": "The full key cannot be shown again after closing this window.", "有效 Key": "Active keys",
    "账户安全": "Account security", "原密码": "Current password", "新密码": "New password", "更新密码": "Update password",
    "找回密码": "Password reset", "注册邮箱": "Account email", "设置新密码": "Set a new password",
    "免费验证候选邮箱": "Verify candidates free", "Developer API": "Developer API", "API Key": "API key", "通知": "Notifications", "暂无通知": "No notifications", "暂无任务": "No recent jobs", "名字": "First name", "姓氏": "Last name", "公司域名": "Company domain",
    "使用 API Key 从你的应用提交邮箱验证任务。完整 Key 只会显示一次。": "Use an API key to submit verification jobs from your application. The full key is shown once.",
    "暂不支持 Yahoo 邮箱验证（含所有国家或地区后缀、ymail.com、rocketmail.com）。Yahoo 的反验证策略非常严格，当前全网常规验证均难以稳定通过，暂时没有可靠解决方案。": "Yahoo email verification is not supported, including regional Yahoo domains, ymail.com, and rocketmail.com. Yahoo's anti-verification controls do not currently permit reliable validation.",
    "暂不支持 Yahoo 邮箱验证（含所有国家或地区后缀，以及 ymail.com、rocketmail.com）。Yahoo 的反验证策略非常严格，当前全网常规验证都难以稳定通过，暂时没有可靠解决方案。": "Yahoo email verification is not supported, including regional Yahoo domains, ymail.com, and rocketmail.com. Yahoo's anti-verification controls do not currently permit reliable validation.",
    "此操作会删除账户、任务记录、额度流水和可下载结果，无法撤销。正在处理的任务必须先完成。": "This permanently deletes your account, jobs, credit history, and downloadable results. Active jobs must finish first.",
    "检测到 QQ 邮箱：将采用专属低并发与自动退避策略，验证速度会较慢，请耐心等待。": "QQ email detected. Verification uses dedicated low concurrency and automatic backoff, so it may take longer.",
    "QQ 专属低并发": "QQ low concurrency", "自定义模式": "Custom mode", "正在解析…": "Parsing...", "选择文件": "Choose file",
    "正在提交…": "Submitting...", "请输入一个邮箱地址": "Enter one email address", "请至少输入一个邮箱地址": "Enter at least one email address", "单个验证一次只能提交一个邮箱地址": "Single check accepts one email address at a time",
    "没有符合条件的结果": "No results match the current filters", "正在等待首条验证结果": "Waiting for the first verification result", "下载失败": "Download failed", "未验证": "Not verified", "正在生成验证结果": "Generating verification results",
    "验证已停止，已保留当前结果。": "Verification stopped. Current results were kept.", "正在从候选地址中确认结果": "Confirming results from candidate addresses", "已找到": "Found", "等待验证": "Waiting for verification",
    "加载中...": "Loading...", "还没有 API Key。": "No API keys yet.", "撤销": "Revoke", "已复制": "Copied", "尚未使用": "Not used yet",
    "请先登录管理员账户": "Sign in with an administrator account", "请先登录后使用工作邮箱查找": "Sign in to use work email discovery", "请先登录后使用企业邮箱查找": "Sign in to use company email finder", "收费批量验证": "Paid bulk verification", "批量验证收件地址": "Verify emails in bulk", "创建账户": "Create account", "例如：生产环境": "e.g. Production",
    "腾讯 QQ 验证节点正在启动，请稍候": "Tencent QQ verification node is starting. Please wait.", "腾讯 QQ 验证节点正在重启，请稍候": "Tencent QQ verification node is restarting. Please wait.",
    "腾讯 QQ 验证节点启动超时，请稍后重新提交": "Tencent QQ verification node timed out while starting. Please submit again later.", "腾讯 QQ 验证节点启动失败，请稍后重新提交": "Tencent QQ verification node failed to start. Please submit again later."
  };

  const backend = {
    "任务不存在": "Job not found", "任务不存在或服务已重启": "Job not found or the service restarted", "请先登录": "Sign in first",
    "请输入有效的邮箱地址": "Enter a valid email address", "请先验证注册邮箱": "Verify your account email first",
    "任务已结束，无法停止": "This job has already ended and cannot be stopped", "只有已停止的任务可以继续验证": "Only stopped jobs can be resumed",
    "结果文件尚未生成": "The result file is not ready", "文件不能超过 5 MB": "The file cannot exceed 5 MB",
    "文件中没有识别到邮箱地址": "No email addresses were found in this file", "该任务没有可继续验证的邮箱": "This job has no remaining emails to verify",
    "请使用浏览器登录会话管理 API Key": "Use a browser sign-in session to manage API keys", "API Key 不存在或已撤销": "API key does not exist or has been revoked",
    "每个账户最多保留 10 个有效 API Key": "Each account can have up to 10 active API keys", "请输入 API Key 名称": "Enter an API key name",
    "邮箱服务器验证": "Mail-server validation", "QQ 头像辅助证据": "QQ avatar evidence", "Outlook 账号验证": "Outlook account validation",
    "Outlook 邮箱已确认可投递": "Outlook email confirmed deliverable", "Outlook 邮箱不可投递": "Outlook email undeliverable", "Outlook 邮箱暂时无法确认": "Outlook email could not be confirmed yet",
    "域名通用收件": "Catch-all domain", "不支持验证": "Unsupported validation", "已停止": "Stopped",
    "已找到可投递邮箱，未继续验证": "A deliverable email was found; remaining candidates were not checked", "验证未返回结果": "Verification returned no result",
    "域名不存在": "Domain does not exist", "没有邮箱服务器": "No mail server found", "250 可投递": "250 Deliverable", "550 不可投递": "550 Undeliverable",
    "邮箱服务器拒绝验证": "Mail server rejected validation", "邮箱服务器暂时无法确认": "Mail server could not confirm delivery yet",
    "请先登录后管理 API Key": "Sign in to manage API keys"
  };

  let locale = localStorage.getItem("verigo_locale") === "en" ? "en" : "zh";

  function localizeText(value) {
    const text = String(value || "");
    if (locale !== "en") return text;
    const nativeEnglish = {
      "邮箱验证": "Email verification", "邮箱查找": "Email finder", "历史记录": "History", "价格": "Pricing",
      "额度与账单": "Credits & billing", "验证邮箱是否可投递": "Check if an email is deliverable",
      "在发送邮件或导入客户名单前，快速识别无效邮箱，减少退信和无效触达。": "Identify invalid emails before sending or importing lists.",
      "每天免费验证": "Free checks daily", "无需信用卡": "No credit card required", "支持批量名单": "Bulk lists supported",
      "验证任务": "Verification job", "复制可投递": "Copy deliverable", "复制不可投递": "Copy undeliverable", "复制无法确认": "Copy unconfirmed", "复制全部": "Copy all",
      "状态": "Status", "查看详情": "View details", "结果详情": "Result details", "邮箱详情": "Email details",
      "可以通过 API 集成吗？": "Can I integrate through the API?",
      "可以。已登录账户可在产品中创建 API Key，并通过 API 文档将验证能力接入 CRM、表单或内部流程。": "Yes. Signed-in accounts can create API keys and connect verification to a CRM, form, or internal workflow.",
      "Verigo · 邮箱验证、名单清洗与企业邮箱查找": "Verigo · Email verification, list cleaning, and company email finder",
    };
    if (nativeEnglish[text]) return nativeEnglish[text];
    if (english[text] || backend[text]) return english[text] || backend[text];
    if (/^(\d+) \/ (\d+) 已处理$/.test(text)) return text.replace(/^(\d+) \/ (\d+) 已处理$/, "$1 / $2 processed");
    if (/^排队中，前方还有 (\d+) 个任务$/.test(text)) return text.replace(/^排队中，前方还有 (\d+) 个任务$/, "Queued; $1 job(s) ahead");
    if (/^已显示 (\d+)-(\d+)，共 (\d+) 个邮箱$/.test(text)) return text.replace(/^已显示 (\d+)-(\d+)，共 (\d+) 个邮箱$/, "Showing $1-$2 of $3 email addresses");
    if (/^(\d+) 秒$/.test(text)) return text.replace(/^(\d+) 秒$/, "$1 sec");
    if (/^(\d+) 次$/.test(text)) return text.replace(/^(\d+) 次$/, "$1 verifications");
    if (/^(\d+) 笔已支付订单$/.test(text)) return text.replace(/^(\d+) 笔已支付订单$/, "$1 paid orders");
    if (/^(\d+) 次 ¥([\d.]+)$/.test(text)) return text.replace(/^(\d+) 次 ¥([\d.]+)$/, "$1 checks ¥$2");
    if (/^最近更新：(.+)$/.test(text)) return text.replace(/^最近更新：(.+)$/, "Updated: $1");
    if (/^更新于 (.+)$/.test(text)) return text.replace(/^更新于 (.+)$/, "Updated: $1");
    if (/^互动率 ([\d.]+)%$/.test(text)) return text.replace(/^互动率 ([\d.]+)%$/, "Engagement rate $1%");
    if (/^会话转化 ([\d.]+)%$/.test(text)) return text.replace(/^会话转化 ([\d.]+)%$/, "Session conversion $1%");
    if (/^共 (\d+) 个账户，按注册时间排序$/.test(text)) return text.replace(/^共 (\d+) 个账户，按注册时间排序$/, "$1 accounts, sorted by registration date");
    if (/^付费 (\d+)$/.test(text)) return text.replace(/^付费 (\d+)$/, "Paid $1");
    if (/^体验 (\d+)$/.test(text)) return text.replace(/^体验 (\d+)$/, "Trial $1");
    if (/^已用 (\d+)$/.test(text)) return text.replace(/^已用 (\d+)$/, "Used $1");
    if (/^另有 (\d+) 体验次数$/.test(text)) return text.replace(/^另有 (\d+) 体验次数$/, "$1 trial credits");
    if (/^(\d+) 邮箱服务器拒绝验证$/.test(text)) return text.replace(/^(\d+) 邮箱服务器拒绝验证$/, "$1 Mail server rejected validation");
    if (/^(\d+) 邮件服务器临时灰名单，正在重试$/.test(text)) return text.replace(/^(\d+) 邮件服务器临时灰名单，正在重试$/, "$1 Mail server greylisted this request; retrying");
    if (/^(\d+) 邮件服务器暂时无法确认，正在重试$/.test(text)) return text.replace(/^(\d+) 邮件服务器暂时无法确认，正在重试$/, "$1 Mail server could not confirm yet; retrying");
    if (/^(\d+) 收件箱容量已满，当前无法接收邮件$/.test(text)) return text.replace(/^(\d+) 收件箱容量已满，当前无法接收邮件$/, "$1 Mailbox is full and cannot receive email right now");
    if (/^(\d+) 收件箱容量已满，需要清理容量后才能接收邮件$/.test(text)) return text.replace(/^(\d+) 收件箱容量已满，需要清理容量后才能接收邮件$/, "$1 Mailbox is full; space must be cleared before it can receive email");
    if (/^(\d+) 服务器连续 3 次未能确认，当前不可投递$/.test(text)) return text.replace(/^(\d+) 服务器连续 3 次未能确认，当前不可投递$/, "$1 Mail server could not confirm after 3 attempts; currently undeliverable");
    if (/^(\d+) 个邮箱$/.test(text)) return text.replace(/^(\d+) 个邮箱$/, "$1 email addresses");
    if (/^开始验证 · (\d+) 额度$/.test(text)) return text.replace(/^开始验证 · (\d+) 额度$/, "Start verification · $1 credits");
    if (/^(.*)；QQ 邮箱采用低并发和自动退避策略，请耐心等待。$/.test(text)) return `${localizeText(text.replace(/^(.*)；QQ 邮箱采用低并发和自动退避策略，请耐心等待。$/, "$1"))}; QQ email uses low concurrency and automatic backoff. Please wait.`;
    if (/^查找 (\d+) 个候选邮箱$/.test(text)) return text.replace(/^查找 (\d+) 个候选邮箱$/, "Finding $1 candidate email addresses");
    if (/^(\d+) 个候选邮箱$/.test(text)) return text.replace(/^(\d+) 个候选邮箱$/, "$1 candidate email addresses");
    if (/^免费验证候选邮箱 · (\d+) 个地址$/.test(text)) return text.replace(/^免费验证候选邮箱 · (\d+) 个地址$/, "Verify $1 candidate email addresses free");
    if (/^已生成 (\d+) 个候选地址。QQ 邮箱验证采用专属低并发策略，验证速度较慢，请耐心等待。$/.test(text)) return text.replace(/^已生成 (\d+) 个候选地址。QQ 邮箱验证采用专属低并发策略，验证速度较慢，请耐心等待。$/, "Generated $1 candidate addresses. QQ email verification uses dedicated low concurrency, so it may take longer.");
    if (/^已生成 (\d+) 个候选地址$/.test(text)) return text.replace(/^已生成 (\d+) 个候选地址$/, "Generated $1 candidate addresses");
    if (/^已找到唯一可确认邮箱：(.+)$/.test(text)) return text.replace(/^已找到唯一可确认邮箱：(.+)$/, "One confirmed email address found: $1");
    if (/^找到 (\d+) 个可确认地址，请结合职位或公开信息进一步确认。$/.test(text)) return text.replace(/^找到 (\d+) 个可确认地址，请结合职位或公开信息进一步确认。$/, "Found $1 plausible addresses. Confirm with role or public information.");
    if (/^没有可确认地址，部分候选暂时无法确认。请稍后重试或检查域名。$/.test(text)) return "No addresses could be confirmed. Some candidates are temporarily inconclusive; try again later or check the domain.";
    if (/^未找到可确认地址。请检查姓名和域名，或对方可能已离职。$/.test(text)) return "No addresses could be confirmed. Check the name and domain, or the person may no longer be with the company.";
    if (/^腾讯 QQ 验证节点启动失败，正在重试（(\d+)\/(\d+)）$/.test(text)) return text.replace(/^腾讯 QQ 验证节点启动失败，正在重试（(\d+)\/(\d+)）$/, "Tencent QQ verification node failed to start; retrying ($1/$2)");
    if (/^腾讯 QQ 验证节点失败: (.+)$/.test(text)) return text.replace(/^腾讯 QQ 验证节点失败: (.+)$/, "Tencent QQ verification node failed: $1");
    return text;
  }

  function text(value) { return localizeText(value); }
  function resultValue(value) {
    const source = String(value || "");
    const localized = localizeText(source);
    if (locale !== "en" || localized !== source || !/[\u4e00-\u9fff]/.test(source)) return localized;
    const smtpCode = source.match(/\b([245]\d{2})\b/)?.[1];
    return smtpCode ? `Mail-server response (${smtpCode})` : "Mail-server response could not be classified";
  }
  function errorMessage(value) {
    const source = String(value || "");
    const localized = localizeText(source);
    return locale === "en" && localized === source && /[\u4e00-\u9fff]/.test(source)
      ? "The request could not be completed"
      : localized;
  }

  function notificationTitle(notification) {
    if (locale !== "en") return notification.title;
    if (notification.kind === "credit_grant") return "Credits added";
    if (notification.kind === "credit_deduction") return "Credits adjusted";
    const localized = localizeText(notification.title);
    return /[\u4e00-\u9fff]/.test(localized) ? "Account notification" : localized;
  }

  function notificationBody(notification) {
    if (locale !== "en") return notification.body;
    const amount = String(notification.body || "").match(/(\d[\d,]*)\s*额度/)?.[1];
    if (notification.kind === "credit_grant" && amount) return `An administrator added ${amount} credits to your account.`;
    if (notification.kind === "credit_deduction" && amount) return `An administrator deducted ${amount} credits from your account.`;
    const localized = localizeText(notification.body);
    return /[\u4e00-\u9fff]/.test(localized) ? "An account update is available." : localized;
  }

  function formatDate(value) { return new Date(value).toLocaleString(locale === "en" ? "en-US" : "zh-CN"); }

  function localizeElement(element) {
    if (!(element instanceof HTMLElement) || element.matches("script, style, #locale-code")) return;
    if (element.children.length) {
      [...element.childNodes]
        .filter((node) => node.nodeType === Node.TEXT_NODE && node.nodeValue.trim())
        .forEach((node, index) => {
          const key = `i18nNode${index}`;
          if (!element.dataset[key]) element.dataset[key] = node.nodeValue;
          const source = element.dataset[key];
          const nextText = locale === "en" ? localizeText(source) : source;
          if (node.nodeValue !== nextText) node.nodeValue = nextText;
        });
    }
    if (!element.children.length) {
      if (!element.dataset.i18nText) element.dataset.i18nText = element.textContent;
      let source = element.dataset.i18nText;
      let localized = localizeText(source);
      if (
        /[\u4e00-\u9fff]/.test(element.textContent)
        && element.textContent !== source
        && element.textContent !== localized
      ) {
        element.dataset.i18nText = element.textContent;
        source = element.dataset.i18nText;
        localized = localizeText(source);
      }
      const isStaticText = element.textContent === source || element.textContent === localized;
      if (isStaticText) {
        const nextText = locale === "en" ? localized : source;
        if (element.textContent !== nextText) element.textContent = nextText;
      }
    }
    ["placeholder", "title", "aria-label"].forEach((name) => {
      if (!element.hasAttribute(name)) return;
      const key = `i18n${name[0].toUpperCase()}${name.slice(1).replace("-", "")}`;
      if (!element.dataset[key]) element.dataset[key] = element.getAttribute(name);
      const nextValue = locale === "en" ? localizeText(element.dataset[key]) : element.dataset[key];
      if (element.getAttribute(name) !== nextValue) element.setAttribute(name, nextValue);
    });
  }

  function localizeTree(root = document.body) {
    if (!root) return;
    localizeElement(root);
    root.querySelectorAll?.("*").forEach(localizeElement);
  }

  function apply() {
    document.documentElement.lang = locale === "en" ? "en" : "zh-CN";
    document.title = locale === "en" ? "Verigo | Email Verification, List Cleaning & Company Email Finder" : "Verigo | 邮箱验证、名单清洗与企业邮箱查找";
    const description = document.querySelector('meta[name="description"]');
    if (description) description.content = locale === "en" ? "Verify email addresses, clean lists, and find company email addresses." : "Verigo 帮助销售、招聘与出海团队验证邮箱、清洗名单，并查找目标企业的可联系邮箱。";
    const code = document.getElementById("locale-code");
    const toggle = document.getElementById("locale-toggle");
    if (toggle) {
      const label = locale === "en" ? "Switch to Chinese" : "Switch to English";
      toggle.setAttribute("aria-label", label);
      toggle.title = label;
    }
    localizeTree();
    if (code) code.textContent = locale === "en" ? "EN" : "CN";
  }

  function set(nextLocale) {
    locale = nextLocale === "en" ? "en" : "zh";
    localStorage.setItem("verigo_locale", locale);
    apply();
    window.dispatchEvent(new CustomEvent("verigo:localechange", { detail: { locale } }));
  }

  function init() {
    apply();
    document.getElementById("locale-toggle")?.addEventListener("click", () => set(locale === "en" ? "zh" : "en"));
    new MutationObserver((records) => {
      records.forEach((record) => record.addedNodes.forEach((node) => {
        if (node.nodeType === Node.ELEMENT_NODE) localizeTree(node);
        if (node.nodeType === Node.TEXT_NODE) localizeElement(node.parentElement);
      }));
    }).observe(document.body, { childList: true, subtree: true });
  }

  return { init, set, text, resultValue, errorMessage, notificationTitle, notificationBody, formatDate, get locale() { return locale; } };
})();
