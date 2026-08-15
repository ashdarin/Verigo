from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
OUTLOOK_HTTP_DOMAINS = {
    'passport.com', 'windowslive.com',
}

# 🆕 微软个人邮箱的域名前缀: 凡是以这些前缀开头的域名(任意国家后缀)都归微软体系。
# 例如 hotmail.com / hotmail.co.uk / hotmail.com.au / live.com.au / outlook.fr / msn.cn 等
# 全部自动覆盖, 无需手工枚举每个国家后缀。
# 经接口实测: 这四个前缀对应的都是消费者(个人)邮箱域名, 接口能正确区分真伪。
OUTLOOK_DOMAIN_PREFIXES = ('hotmail.', 'outlook.', 'live.', 'msn.')


def is_outlook_domain(domain):
    """🆕 判断域名是否属于微软(Outlook)账号体系。
    前缀匹配 + 显式清单, 自动覆盖所有国家后缀(.co.uk / .com.au / .fr / .de ...)。"""
    d = domain.lower().strip()
    if d in OUTLOOK_HTTP_DOMAINS:
        return True
    return d.startswith(OUTLOOK_DOMAIN_PREFIXES)

_MS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_ms_ssl_ctx = ssl.create_default_context()
MS_HTTP_TIMEOUT = 15
MS_RETRY_ON_THROTTLE = 3
MS_BACKOFF_BASE = 3.0


def _timed_query(query, email):
    started = time.monotonic()
    return query(email), round((time.monotonic() - started) * 1000, 2)


def _ms_query_getcredtype(email):
    """微软接口A: GetCredentialType。返回 {'ok','exists','throttled','detail'}。
    IfExistsResult: 0/5/6=存在, 1=不存在, 其它=未知。"""
    url = "https://login.microsoftonline.com/common/GetCredentialType?mkt=en-US"
    body = json.dumps({
        "username": email, "isOtherIdpSupported": True, "checkPhones": False,
        "isRemoteNGCSupported": True, "isCookieBannerShown": False,
        "isFidoSupported": True, "originalRequest": "", "country": "US",
    }).encode()
    headers = {
        "User-Agent": _MS_UA, "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json", "Origin": "https://login.microsoftonline.com",
        "Referer": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    }
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=MS_HTTP_TIMEOUT, context=_ms_ssl_ctx) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        ifx = data.get("IfExistsResult")
        throttle = data.get("ThrottleStatus", 0)
        throttled = throttle not in (0, None)
        if ifx in (0, 5, 6):
            exists = True
        elif ifx == 1:
            exists = False
        else:
            exists = None
        return {"ok": True, "exists": exists, "throttled": throttled,
                "detail": f"IfExistsResult={ifx}"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "exists": None, "throttled": e.code == 429, "detail": f"HTTP{e.code}"}
    except Exception as e:
        return {"ok": False, "exists": None, "throttled": False, "detail": type(e).__name__}


def _ms_query_odc(email):
    """微软接口B: Office ODC idp。account: MSAccount/OrgId/Both=存在, Neither=不存在。"""
    url = ("https://odc.officeapps.live.com/odc/v2.1/idp?hm=0&emailAddress="
           + urllib.parse.quote(email))
    headers = {"User-Agent": _MS_UA, "Accept": "application/json"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=MS_HTTP_TIMEOUT, context=_ms_ssl_ctx) as r:
            raw = r.read().decode("utf-8", "replace")
        acct = None
        try:
            j = json.loads(raw)
            acct = j.get("account") or j.get("Account")
        except Exception:
            mm = re.search(r"[Aa]ccount[\"'>:\s]+([A-Za-z]+)", raw)
            acct = mm.group(1) if mm else None
        if acct in ("MSAccount", "OrgId", "Both"):
            exists = True
        elif acct == "Neither":
            exists = False
        else:
            exists = None
        return {"ok": True, "exists": exists, "throttled": False, "detail": f"account={acct}"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "exists": None, "throttled": e.code == 429, "detail": f"HTTP{e.code}"}
    except Exception as e:
        return {"ok": False, "exists": None, "throttled": False, "detail": type(e).__name__}


def verify_outlook_via_microsoft(email, *, include_timings: bool = False):
    """🆕 用微软官方接口交叉验证 Outlook 邮箱是否存在。
    返回 (exists, detail):
      exists=True   两接口都判存在 / 一个存在另一个未知
      exists=False  两接口都判不存在 / 一个不存在另一个未知
      exists=None   两接口分歧 / 均未知 / 持续被限流  (不下结论, 避免误杀)
    detail: 人类可读的说明字符串(写入CSV的"SMTP结果码"列)。"""
    timings: dict[str, float] = {}
    a = b = None
    for attempt in range(MS_RETRY_ON_THROTTLE):
        # These endpoints are independent confirmation signals. Running them
        # together shortens one mailbox check without increasing request count.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="outlook-query") as pool:
            credential_future = pool.submit(_timed_query, _ms_query_getcredtype, email)
            odc_future = pool.submit(_timed_query, _ms_query_odc, email)
            a, timings["credential_type"] = credential_future.result()
            b, timings["odc"] = odc_future.result()
        if a["throttled"] or b["throttled"]:
            started = time.monotonic()
            time.sleep(MS_BACKOFF_BASE * (2 ** attempt))
            timings["backoff"] = round(
                timings.get("backoff", 0.0) + (time.monotonic() - started) * 1000,
                2,
            )
            continue
        break

    ea, eb = a["exists"], b["exists"]

    if ea is True and eb is True:
        outcome = True, "Outlook 邮箱已确认可投递"
    elif ea is False and eb is False:
        outcome = False, "Outlook 邮箱不可投递"
    elif {ea, eb} == {True, None}:
        outcome = True, "Outlook 邮箱已确认可投递"
    elif {ea, eb} == {False, None}:
        outcome = False, "Outlook 邮箱不可投递"
    else:
        outcome = None, "Outlook 邮箱暂时无法确认"
    if include_timings:
        return *outcome, timings
    return outcome

# 🆕 全局共享的域名类型缓存（跨进程共享）
