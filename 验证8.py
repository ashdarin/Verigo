#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全自动分布式邮箱验证工具 - 验证4自动保存版
基于验证2.py的完整功能，专门解决QQ和Outlook邮箱验证问题

特点：
- 保持验证2.py的所有原有功能不变
- 用户完全控制进程数（1-8个）
- 保持BMW/Audi激进策略不变
- 🆕 专门修复QQ邮箱和Outlook邮箱的验证问题
- 🆕 针对消费者邮箱的优化SMTP策略
- 完整的表情符号和视觉效果
- 🔧 自动导出CSV结果，无需手动确认
- 🔧 自动保存域名缓存，验证完成即保存
"""

import socket
import re
import csv
import io
import sys
import time
import smtplib
import imaplib
import email as email_module
import dns.exception
import dns.resolver
import threading
import json
import os
import math
import signal
import ssl
import random
import string
import urllib.request
import urllib.error
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from multiprocessing import Process, Queue, Manager, cpu_count
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from queue import Empty
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

from app.config import settings
from app.core.qq_evidence import qq_avatar_evidence
from app.core.smtp_limiter import SMTPDeliveryLimiter


SMTP_MAX_CONCURRENT_PER_MX = max(1, int(os.getenv('VERIGO_SMTP_PER_MX', '8')))
SMTP_HELO_HOST = settings.smtp_helo_host
SMTP_MAIL_FROM = settings.smtp_mail_from


def smtp_gate_capacity(mx_host):
    """Keep the full job concurrency except for QQ's more sensitive MX hosts."""
    host = str(mx_host).lower().rstrip('.')
    if host.endswith('.qq.com') or host.endswith('.foxmail.com'):
        return min(SMTP_MAX_CONCURRENT_PER_MX, settings.qq_smtp_per_mx)
    return SMTP_MAX_CONCURRENT_PER_MX

# ============================================================================
# 🆕 Outlook 体系邮箱验证 —— 微软官方账号接口 (HTTPS, 非 SMTP)
# ----------------------------------------------------------------------------
# 背景: Outlook/Hotmail 的 SMTP 验证依赖出口 IP 信誉, 在被 Spamhaus/微软拉黑或
# 云平台(GCP/Azure)封禁 25 端口时, MAIL FROM 阶段即被拒, 真假邮箱返回相同结果,
# 导致"先准后崩"和大量误判。
# 解决: 改走微软登录/Office 后台用于判断"账号是否存在"的官方接口, 走 HTTPS 443,
# 不碰 25 端口、不受 IP 黑名单影响、不花钱、不用 API key。经已知有效/无效邮箱实测,
# 双接口信号一致、准确率高:
#   存在  : GetCredentialType.IfExistsResult ∈ {0,5,6}  且  ODC.account == 'MSAccount'
#   不存在: GetCredentialType.IfExistsResult == 1        且  ODC.account == 'Neither'
# 判定(永不误杀): 两接口一致才下确定结论; 分歧/限流/异常一律记为"未知"。
# ============================================================================

# Outlook 体系域名(共用微软账号体系) —— 命中即走 HTTP 接口验证, 不走 SMTP
# 显式清单: 一些不带标准前缀但仍属微软个人邮箱的域名
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


def verify_outlook_via_microsoft(email):
    """🆕 用微软官方接口交叉验证 Outlook 邮箱是否存在。
    返回 (exists, detail):
      exists=True   两接口都判存在 / 一个存在另一个未知
      exists=False  两接口都判不存在 / 一个不存在另一个未知
      exists=None   两接口分歧 / 均未知 / 持续被限流  (不下结论, 避免误杀)
    detail: 人类可读的说明字符串(写入CSV的"SMTP结果码"列)。"""
    a = b = None
    for attempt in range(MS_RETRY_ON_THROTTLE):
        a = _ms_query_getcredtype(email)
        b = _ms_query_odc(email)
        if a["throttled"] or b["throttled"]:
            time.sleep(MS_BACKOFF_BASE * (2 ** attempt))
            continue
        break

    ea, eb = a["exists"], b["exists"]
    raw = f"[接口A:{a['detail']} | 接口B:{b['detail']}]"

    if ea is True and eb is True:
        return True, f"微软接口确认账号存在 {raw}"
    if ea is False and eb is False:
        return False, f"微软接口确认账号不存在 {raw}"
    if {ea, eb} == {True, None}:
        return True, f"微软接口确认账号存在(单接口) {raw}"
    if {ea, eb} == {False, None}:
        return False, f"微软接口确认账号不存在(单接口) {raw}"
    # 分歧 或 双未知/限流 -> 不下结论
    if None not in (ea, eb):
        return None, f"两接口结果分歧,无法判定 {raw}"
    return None, f"接口未返回明确结果(可能被限流) {raw}"

# 🆕 全局共享的域名类型缓存（跨进程共享）
_global_domain_type_cache = {}
_global_domain_type_cache_lock = threading.Lock()

# 🔧 持久化缓存文件路径
DOMAIN_CACHE_FILE = "domain_type_cache.json"
DOMAIN_CACHE_TTL_DAYS = 7  # 缓存有效期7天

def load_persistent_cache():
    """从文件加载持久化缓存"""
    global _global_domain_type_cache
    try:
        if os.path.exists(DOMAIN_CACHE_FILE):
            with open(DOMAIN_CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 转换时间字符串为datetime对象，并过滤过期条目
                now = datetime.now()
                for domain, entry in data.items():
                    try:
                        checked_at = datetime.fromisoformat(entry['checked_at'])
                        if now - checked_at < timedelta(days=DOMAIN_CACHE_TTL_DAYS):
                            _global_domain_type_cache[domain] = {
                                'type': entry['type'],
                                'checked_at': checked_at
                            }
                    except:
                        pass
                print(f"📂 已加载 {len(_global_domain_type_cache)} 条域名缓存")
    except Exception as e:
        print(f"⚠️ 加载缓存文件失败: {e}")

def save_persistent_cache():
    """保存缓存到文件"""
    global _global_domain_type_cache
    try:
        with _global_domain_type_cache_lock:
            # 转换datetime为字符串以便JSON序列化
            data = {}
            for domain, entry in _global_domain_type_cache.items():
                data[domain] = {
                    'type': entry['type'],
                    'checked_at': entry['checked_at'].isoformat()
                }
            with open(DOMAIN_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存缓存文件失败: {e}")

def get_shared_domain_type(domain):
    """获取共享的域名类型缓存"""
    with _global_domain_type_cache_lock:
        if domain in _global_domain_type_cache:
            cache_entry = _global_domain_type_cache[domain]
            if datetime.now() - cache_entry['checked_at'] < timedelta(days=DOMAIN_CACHE_TTL_DAYS):
                return cache_entry['type']
    return None

def set_shared_domain_type(domain, domain_type):
    """设置共享的域名类型缓存"""
    with _global_domain_type_cache_lock:
        _global_domain_type_cache[domain] = {
            'type': domain_type,
            'checked_at': datetime.now()
        }

# ============================================================================
# 🆕 邮箱字符检测 —— 检测空格 / 非法字符 (用于在验证前直接剔除问题邮箱)
# ----------------------------------------------------------------------------
# 合法邮箱本地部分+域名允许的字符集: 字母、数字、. _ % + - 以及分隔符 @
# 任何空白字符(空格/制表/换行)或上述集合之外的字符都视为"非法字符"。
# ============================================================================
def check_email_characters(raw_email):
    """检测邮箱字符串中的空格和非法字符。
    返回 (is_clean, detail):
      is_clean=True  无任何问题, detail='正常'
      is_clean=False detail 为分号分隔的问题清单(如 '内部空格; 非ASCII字符')

    注意: 首尾空格不算问题(调用方会先 strip), 只判定 strip 之后仍存在的问题。"""
    raw = '' if raw_email is None else str(raw_email)
    stripped = raw.strip()
    issues = []

    # 1) 内部空白字符(空格/制表符等) —— strip 后仍有空白说明地址中间有空格
    if re.search(r'\s', stripped):
        issues.append('内部空格')

    # 2) 非ASCII字符(中文、全角符号等)
    if any(ord(c) > 127 for c in stripped):
        issues.append('非ASCII字符')

    # 3) 其它非法字符(排除空白, 空白已在上面单独统计)
    illegal = [c for c in re.findall(r"[^A-Za-z0-9.@_%+\-]", stripped)
               if not c.isspace() and ord(c) <= 127]
    if illegal:
        uniq = ''.join(sorted(set(illegal)))
        issues.append(f'非法字符:{uniq}')

    if issues:
        return False, '; '.join(issues)
    return True, '正常'


class EmailVerifier:
    """核心邮箱验证器 - 保持原有逻辑不变，增加QQ和Outlook修复"""
    
    def __init__(self):
        # 保持原有的精确域名分类策略
        self.consumer_domains = {
            'gmail.com', 'outlook.com', 'hotmail.com', 'yahoo.com', 'icloud.com',
            'qq.com', '163.com', '126.com', 'sina.com', 'sohu.com', 'foxmail.com',
            'live.com', 'msn.com', 'yahoo.co.uk', 'yahoo.de', 'gmx.de', 'web.de'
        }

        # 🆕 域名类型缓存 - 避免重复检测catch-all
        self.domain_type_cache = {}  # 格式: {'domain': {'type': 'catch-all'/'normal'/'consumer', 'checked_at': datetime}}

        # 🆕 Google Cloud环境专用QQ和Outlook修复策略
        self.consumer_fix_strategies = {
            # QQ邮箱优化策略 - 基于RCPT TO验证，避免DMARC问题
            'qq.com': {
                'provider': 'QQ',
                'timeout': 25,
                'max_attempts': 1,
                'mx_delay': 1.5,
                'max_mx_hosts': 1,
                'helo_domains': [
                    SMTP_HELO_HOST
                ],
                'sender_emails': [
                    SMTP_MAIL_FROM
                ],
                'strategy_type': 'qq_optimized',
                'use_expn_command': False,  # � 禁用：不需要
                'use_vrfy_command': False,  # � 禁用：不需要
                'use_ehlo': True,           # � 保持：成功配置
                'try_multiple_ports': False, # � 禁用：端口25成功
                'ports': [25],              # � 只用端口25
                'use_data_command': False,
                'special_handling': True
            },
            'vip.qq.com': {
                'provider': 'QQ_VIP',
                'timeout': 25,
                'max_attempts': 1,
                'mx_delay': 1.5,    # 🎯 优化：减少延迟
                'max_mx_hosts': 1,
                'helo_domains': [
                    SMTP_HELO_HOST
                ],
                'sender_emails': [
                    SMTP_MAIL_FROM
                ],
                'strategy_type': 'qq_optimized',
                'use_expn_command': False,
                'use_vrfy_command': False,
                'use_ehlo': True,
                'try_multiple_ports': False,
                'ports': [25],
                'use_data_command': False,
                'special_handling': True
            },
            'foxmail.com': {
                'provider': 'Foxmail',
                'timeout': 25,
                'max_attempts': 1,
                'mx_delay': 1.5,    # 🎯 优化：减少延迟
                'max_mx_hosts': 1,
                'helo_domains': [
                    SMTP_HELO_HOST
                ],
                'sender_emails': [
                    SMTP_MAIL_FROM
                ],
                'strategy_type': 'qq_optimized',
                'use_expn_command': False,
                'use_vrfy_command': False,
                'use_ehlo': True,
                'try_multiple_ports': False,
                'ports': [25],
                'use_data_command': False,
                'special_handling': True
            },

            # 🔧 已移除 Outlook/Hotmail/Live/MSN 的 SMTP 策略:
            # 这些域名现在改走微软官方账号接口(HTTPS)验证, 不再走 SMTP。
            # 识别与分流逻辑见 verify_email_comprehensive() 中的 OUTLOOK_HTTP_DOMAINS 判断,
            # 实际验证调用模块顶部的 verify_outlook_via_microsoft()。
        }

        # DNS缓存 - 保持原有功能
        self.dns_cache = {}
        self.dns_cache_lock = threading.Lock()
        self.dns_cache_ttl = timedelta(hours=1)

        # 企业域名激进策略 - 完全保持不变
        self.aggressive_domains = {
            'bmw.com', 'bmwgroup.com', 'mini.com',
            'audi.com', 'audi.de', 'audiag.com'
        }
        self.smtp_limiter = SMTPDeliveryLimiter()

    @contextmanager
    def smtp_gate(self, mx_host):
        host = str(mx_host).lower().rstrip('.')
        if host.endswith('.qq.com') or host.endswith('.foxmail.com'):
            # Separate QQ MX records must still share one provider-wide lease.
            with self.smtp_limiter.permit(
                'qq-smtp-global', 1, wait_seconds=settings.qq_smtp_wait_seconds
            ) as global_acquired:
                if not global_acquired:
                    yield False
                    return
                with self.smtp_limiter.permit(
                    mx_host,
                    smtp_gate_capacity(mx_host),
                    wait_seconds=settings.qq_smtp_wait_seconds,
                ) as acquired:
                    yield acquired
            return
        with self.smtp_limiter.permit(mx_host, smtp_gate_capacity(mx_host)) as acquired:
            yield acquired

    def record_smtp_response(self, mx_host, code):
        host = str(mx_host).lower().rstrip('.')
        if 200 <= code < 400:
            self.smtp_limiter.record_success(mx_host)
            if host.endswith('.qq.com') or host.endswith('.foxmail.com'):
                self.smtp_limiter.record_success('qq-smtp-global')
        elif 400 <= code < 500:
            if not (host.endswith('.qq.com') or host.endswith('.foxmail.com')):
                self.smtp_limiter.record_temporary_failure(mx_host)

    def record_smtp_failure(self, mx_host):
        host = str(mx_host).lower().rstrip('.')
        if host.endswith('.qq.com') or host.endswith('.foxmail.com'):
            self.record_qq_policy_failure(mx_host)
            return
        self.smtp_limiter.record_temporary_failure(mx_host)

    def record_qq_policy_failure(self, mx_host):
        for host in (mx_host, 'qq-smtp-global'):
            self.smtp_limiter.record_temporary_failure(
                host,
                base_delay=settings.qq_backoff_base_seconds,
                max_delay=settings.qq_backoff_max_seconds,
            )

    def is_valid_email_format(self, email):
        """邮箱格式验证 - 保持原有逻辑"""
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return re.match(pattern, email) is not None

    def get_domain_strategy(self, domain):
        """获取域名验证策略 - 保持原有逻辑"""
        domain = domain.lower()
        
        if domain in self.aggressive_domains:
            return 'super_aggressive'
        elif domain in self.consumer_domains:
            return 'fast'
        elif domain.endswith(('.edu', '.gov', '.org')):
            return 'medium'
        elif any(keyword in domain for keyword in ['mail', 'email', 'smtp']):
            return 'strict'
        else:
            return 'normal'

    def check_domain_exists(self, domain):
        """Check DNS existence without requiring a website A record."""
        # 🔧 优化：使用DNS缓存检查域名是否已验证过
        cache_key = f"domain_{domain}"
        with self.dns_cache_lock:
            if cache_key in self.dns_cache:
                cached_time, cached_result = self.dns_cache[cache_key]
                if datetime.now() - cached_time < self.dns_cache_ttl:
                    return cached_result
        
        result = False
        try:
            for record_type in ('MX', 'A', 'AAAA', 'NS', 'SOA'):
                try:
                    answers = dns.resolver.resolve(domain, record_type)
                    if answers:
                        result = True
                        break
                except dns.resolver.NXDOMAIN:
                    result = False
                    break
                except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
                    continue
                except dns.exception.DNSException:
                    # A transient resolver failure must not become a false
                    # "domain does not exist" verdict.
                    continue
        except dns.exception.DNSException:
            result = False
        
        # 缓存结果
        with self.dns_cache_lock:
            self.dns_cache[cache_key] = (datetime.now(), result)
        
        return result

    def get_mx_records(self, domain):
        """获取MX记录 - 保持原有DNS缓存逻辑"""
        cache_key = f"mx_{domain}"
        
        with self.dns_cache_lock:
            if cache_key in self.dns_cache:
                cached_time, cached_records = self.dns_cache[cache_key]
                if datetime.now() - cached_time < self.dns_cache_ttl:
                    return cached_records

        try:
            mx_records = []
            answers = dns.resolver.resolve(domain, 'MX')
            for rdata in answers:
                mx_records.append((rdata.preference, str(rdata.exchange).rstrip('.')))
            
            mx_records.sort(key=lambda x: x[0])
            mx_hosts = [mx[1] for mx in mx_records]
            
            with self.dns_cache_lock:
                self.dns_cache[cache_key] = (datetime.now(), mx_hosts)
            
            return mx_hosts
        except Exception:
            return []

    def get_dns_cache_stats(self):
        """获取DNS缓存统计信息 - 保持原有功能"""
        with self.dns_cache_lock:
            return {
                'total_entries': len(self.dns_cache),
                'cache_ttl_hours': self.dns_cache_ttl.total_seconds() / 3600
            }

    def get_consumer_fix_strategy(self, domain):
        """🆕 获取消费者邮箱修复策略"""
        return self.consumer_fix_strategies.get(domain.lower())

    def is_consumer_fix_supported(self, domain):
        """🆕 检查是否为支持修复的消费者邮箱"""
        return domain.lower() in self.consumer_fix_strategies

    def check_smtp_delivery_fixed(self, email, mx_host, fix_strategy):
        """消费者邮箱 SMTP 检查，保留 RCPT TO 判定并记录断开的阶段。"""
        config = fix_strategy
        ports = config.get('ports', [25]) if config.get('try_multiple_ports', False) else [25]
        last_failure = None

        with self.smtp_gate(mx_host) as gate_acquired:
            if not gate_acquired:
                if config['strategy_type'] in ('qq_aggressive', 'qq_optimized'):
                    return None, f"QQ 验证节点正在退避等待: {mx_host}"
                return None, f"SMTP连接排队超时: {mx_host}"

            for attempt in range(config['max_attempts']):
                if attempt:
                    time.sleep(config['mx_delay'])
                port = ports[attempt % len(ports)]
                phase = '建立连接'
                server = None
                try:
                    server = smtplib.SMTP_SSL(timeout=config['timeout']) if port == 465 else smtplib.SMTP(timeout=config['timeout'])
                    phase = '连接'
                    code, _ = server.connect(mx_host, port)
                    self.record_smtp_response(mx_host, code)
                    if code != 220:
                        last_failure = f"连接阶段返回 {code}"
                        continue
                    phase = 'EHLO/HELO'
                    helo_domain = config['helo_domains'][attempt % len(config['helo_domains'])]
                    code, _ = server.ehlo(helo_domain) if config.get('use_ehlo') else server.helo(helo_domain)
                    self.record_smtp_response(mx_host, code)
                    if code != 250:
                        last_failure = f"EHLO/HELO阶段返回 {code}"
                        continue
                    phase = 'MAIL FROM'
                    sender = config['sender_emails'][attempt % len(config['sender_emails'])]
                    code, _ = server.mail(sender)
                    self.record_smtp_response(mx_host, code)
                    if code != 250:
                        last_failure = f"MAIL FROM阶段返回 {code}"
                        continue
                    phase = 'RCPT TO'
                    code, response = server.rcpt(email)
                    self.record_smtp_response(mx_host, code)
                    if config['strategy_type'] in ('qq_aggressive', 'qq_optimized'):
                        if self._is_qq_policy_response(code, response):
                            self.record_qq_policy_failure(mx_host)
                        verdict, message = self._handle_qq_response(code, response, config, attempt)
                        if verdict != 'continue':
                            return verdict, message
                        last_failure = message
                        continue
                    if code == 250:
                        return True, f"250 {config['provider']}邮箱存在"
                    if code == 550:
                        return False, f"550 {config['provider']}邮箱不存在"
                    last_failure = f"RCPT TO阶段返回 {code}"
                except smtplib.SMTPServerDisconnected:
                    self.record_smtp_failure(mx_host)
                    last_failure = f"SMTP连接被服务器关闭（{phase}阶段）"
                except socket.timeout:
                    self.record_smtp_failure(mx_host)
                    last_failure = f"SMTP连接超时（{phase}阶段）"
                except (ConnectionRefusedError, socket.gaierror) as exc:
                    self.record_smtp_failure(mx_host)
                    last_failure = f"SMTP连接失败（{phase}阶段）: {type(exc).__name__}"
                except Exception as exc:
                    self.record_smtp_failure(mx_host)
                    last_failure = f"SMTP错误（{phase}阶段）: {str(exc)[:80]}"
                finally:
                    if server is not None:
                        try:
                            server.quit()
                        except Exception:
                            pass

        if config['strategy_type'] in ('qq_aggressive', 'qq_optimized'):
            return None, f"{config['provider']} SMTP 暂时无法确认: {last_failure or '无有效响应'}"
        return None, f"{config['provider']} SMTP暂时无法确认: {last_failure or '无有效响应'}"

    @staticmethod
    def _is_qq_policy_response(code, response):
        if code in (421, 451, 452, 553, 554):
            return True
        if code != 550:
            return False
        response_text = response.decode('utf-8', 'replace') if isinstance(response, bytes) else str(response)
        response_text = response_text.lower()
        mailbox_missing = (
            'user unknown', 'not found', 'does not exist', 'no such user',
            'invalid recipient', 'recipient unknown', 'user not found',
            'mailbox not found', 'address not found',
        )
        return not any(keyword in response_text for keyword in mailbox_missing)

    def _handle_qq_response(self, code, response, config, attempt):
        """QQ邮箱响应处理：只依据 RCPT TO，不进入 DATA 阶段。"""
        response_text = response.decode('utf-8', 'replace') if isinstance(response, bytes) else str(response)
        if code == 250:
            return True, f"250 {config['provider']} RCPT已接受: {response_text[:160]}"
        elif code == 550:
            response_str = response_text.lower()
            # 检查是否是真正的用户不存在
            if any(keyword in response_str for keyword in [
                'user unknown', 'not found', 'does not exist', 'no such user',
                'invalid recipient', 'recipient unknown', 'user not found',
                'mailbox not found', 'address not found'
            ]):
                return False, f"550 {config['provider']}邮箱不存在: {response_text[:160]}"
            else:
                # QQ的策略保护，继续尝试不同的HELO和发件人
                if attempt < config['max_attempts'] - 1:
                    return 'continue', f"550 {config['provider']}策略保护，等待后重试"
                else:
                    return None, f"550 {config['provider']}策略拒绝，暂时无法确认: {response_text[:160]}"
        elif code in [451, 452, 421]:
            # 临时失败，继续重试
            if attempt < config['max_attempts'] - 1:
                return 'continue', f"{code} {config['provider']}临时失败，重试"
            else:
                return None, f"{code} {config['provider']}临时失败，暂时无法确认"
        elif code in [553, 554]:
            # 邮箱策略拒绝，但继续尝试
            if attempt < config['max_attempts'] - 1:
                return 'continue', f"{code} {config['provider']}策略拒绝，等待后重试"
            else:
                return None, f"{code} {config['provider']}策略拒绝，暂时无法确认: {response_text[:160]}"
        else:
            if attempt < config['max_attempts'] - 1:
                return 'continue', f"{code} {config['provider']}未明确响应，重试"
            return None, f"{code} {config['provider']}响应不明确，暂时无法确认: {response_text[:160]}"

    def _verify_with_expn_command(self, server, email, config):
        """🆕 使用EXPN命令进行邮箱验证 - RFC推荐的方法"""
        try:
            # 首先尝试EXPN命令 - 专门用于验证邮箱地址
            code_expn, response_expn = server.docmd(f'EXPN {email}')

            if code_expn == 250:
                return True, f"250 {config['provider']}邮箱存在(EXPN验证)"
            elif code_expn == 550:
                return False, f"550 {config['provider']}邮箱不存在(EXPN验证)"
            elif code_expn in [251, 252]:
                # 251/252表示无法验证但命令有效，尝试VRFY
                return 'try_vrfy', f"{code_expn} {config['provider']}EXPN无法确定，尝试VRFY"
            else:
                # EXPN不支持，尝试VRFY
                return 'try_vrfy', f"{code_expn} {config['provider']}EXPN不支持，尝试VRFY"
        except Exception as e:
            # EXPN失败，尝试VRFY
            return 'try_vrfy', f"EXPN异常: {str(e)}"

    def _verify_with_vrfy_command(self, server, email, config):
        """🆕 使用VRFY命令进行邮箱验证 - 更准确的方法"""
        try:
            # 尝试VRFY命令
            code_vrfy, response_vrfy = server.docmd(f'VRFY {email}')

            if code_vrfy == 250:
                return True, f"250 {config['provider']}邮箱存在(VRFY验证)"
            elif code_vrfy == 550:
                return False, f"550 {config['provider']}邮箱不存在(VRFY验证)"
            elif code_vrfy in [251, 252]:
                # 251/252表示无法验证但命令有效，继续使用RCPT TO
                return 'continue_rcpt', f"{code_vrfy} {config['provider']}VRFY无法确定，使用RCPT TO"
            else:
                # VRFY不支持，继续使用RCPT TO
                return 'continue_rcpt', f"{code_vrfy} {config['provider']}VRFY不支持，使用RCPT TO"
        except Exception as e:
            # VRFY失败，继续使用RCPT TO
            return 'continue_rcpt', f"VRFY异常: {str(e)}"

    def _verify_with_data_command(self, server, email, config):
        """DATA 验证已禁用；保留方法仅兼容旧配置。"""
        try:
            server.rset()
        except Exception:
            pass
        return None, f"{config['provider']} DATA验证已禁用"

    def detect_catch_all_domain(self, domain):
        """🆕 检测域名是否为catch-all策略 - 优化版：使用共享缓存避免重复检测"""
        import random
        import string

        # 🔧 优先检查本地缓存
        if domain in self.domain_type_cache:
            cache_entry = self.domain_type_cache[domain]
            if datetime.now() - cache_entry['checked_at'] < timedelta(hours=1):
                return cache_entry['type']

        # 🔧 检查全局共享缓存（跨进程）
        shared_type = get_shared_domain_type(domain)
        if shared_type:
            # 同步到本地缓存
            self.domain_type_cache[domain] = {
                'type': shared_type,
                'checked_at': datetime.now()
            }
            return shared_type

        # 🔧 消费者域名直接跳过catch-all检测，使用专门策略
        if domain in self.consumer_domains:
            domain_type = 'consumer'
            self.domain_type_cache[domain] = {
                'type': domain_type,
                'checked_at': datetime.now()
            }
            set_shared_domain_type(domain, domain_type)
            return domain_type

        # 🔧 有专门修复策略的域名也跳过catch-all检测
        if domain in self.consumer_fix_strategies:
            domain_type = 'consumer'
            self.domain_type_cache[domain] = {
                'type': domain_type,
                'checked_at': datetime.now()
            }
            set_shared_domain_type(domain, domain_type)
            return domain_type

        try:
            # 生成随机测试邮箱
            random_prefix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=15))
            test_email = f"test_random_{random_prefix}@{domain}"

            # 🔧 减少日志输出，只在调试时显示
            # print(f"🔍 检测域名 {domain} 是否为catch-all策略...")

            # 获取MX记录（使用缓存）
            mx_records = self.get_mx_records(domain)
            if not mx_records:
                domain_type = 'no_mx'
                self.domain_type_cache[domain] = {
                    'type': domain_type,
                    'checked_at': datetime.now()
                }
                set_shared_domain_type(domain, domain_type)
                return domain_type

            # 使用第一个MX记录进行测试
            mx_host = mx_records[0]

            # 🔧 优化：快速SMTP测试，只需验证一个随机邮箱返回250即可证明是catch-all
            try:
                server = smtplib.SMTP(timeout=5)  # 🔧 优化：减少超时到5秒
                code, response = server.connect(mx_host, 25)
                if code != 220:
                    server.quit()
                    raise Exception(f"连接失败: {code}")

                # EHLO握手
                code, response = server.ehlo(SMTP_HELO_HOST)
                if code != 250:
                    server.quit()
                    raise Exception(f"EHLO失败: {code}")

                # MAIL FROM
                code, response = server.mail(SMTP_MAIL_FROM)
                if code != 250:
                    server.quit()
                    raise Exception(f"MAIL FROM失败: {code}")

                # RCPT TO - 关键测试：随机邮箱返回250就是catch-all
                code, response = server.rcpt(test_email)
                server.quit()

                if code == 250:
                    domain_type = 'catch-all'
                else:
                    domain_type = 'normal'

                # 🔧 缓存结果到本地和全局
                self.domain_type_cache[domain] = {
                    'type': domain_type,
                    'checked_at': datetime.now()
                }
                set_shared_domain_type(domain, domain_type)
                return domain_type

            except Exception as e:
                try:
                    server.quit()
                except:
                    pass
                raise e

        except Exception as e:
            # 检测失败时默认为正常域名
            domain_type = 'normal'
            self.domain_type_cache[domain] = {
                'type': domain_type,
                'checked_at': datetime.now()
            }
            set_shared_domain_type(domain, domain_type)
            return domain_type

    def check_smtp_delivery(self, email, mx_host, strategy):
        """标准 SMTP 检查：同一 MX 串行访问，临时或断连错误重试后判不可投递。"""
        strategy_config = {
            'fast': {'timeout': 8, 'max_attempts': 2, 'mx_delay': 0.8},
            'normal': {'timeout': 15, 'max_attempts': 2, 'mx_delay': 1.0},
            'medium': {'timeout': 15, 'max_attempts': 2, 'mx_delay': 1.2},
            'strict': {'timeout': 20, 'max_attempts': 2, 'mx_delay': 1.5},
            'super_aggressive': {'timeout': 15, 'max_attempts': 2, 'mx_delay': 1.2},
        }
        config = strategy_config.get(strategy, strategy_config['normal'])
        last_failure = None

        with self.smtp_gate(mx_host) as gate_acquired:
            if not gate_acquired:
                return None, f"SMTP连接排队超时: {mx_host}"
            for attempt in range(config['max_attempts']):
                if attempt:
                    time.sleep(config['mx_delay'])
                phase = '建立连接'
                server = None
                try:
                    server = smtplib.SMTP(timeout=config['timeout'])
                    phase = '连接'
                    code, _ = server.connect(mx_host, 25)
                    self.record_smtp_response(mx_host, code)
                    if code != 220:
                        last_failure = f"连接阶段返回 {code}"
                        continue
                    phase = 'HELO'
                    code, _ = server.ehlo(SMTP_HELO_HOST)
                    self.record_smtp_response(mx_host, code)
                    if code != 250:
                        last_failure = f"HELO阶段返回 {code}"
                        continue
                    phase = 'MAIL FROM'
                    code, _ = server.mail(SMTP_MAIL_FROM)
                    self.record_smtp_response(mx_host, code)
                    if code != 250:
                        last_failure = f"MAIL FROM阶段返回 {code}"
                        continue
                    phase = 'RCPT TO'
                    code, response = server.rcpt(email)
                    self.record_smtp_response(mx_host, code)
                    if code == 250:
                        return True, "250 邮箱存在"
                    if code == 550:
                        return False, "550 邮箱不存在"
                    if isinstance(response, bytes):
                        response = response.decode("utf-8", errors="replace")
                    last_failure = f"RCPT TO阶段返回 {code}: {str(response)[:160]}"
                except smtplib.SMTPServerDisconnected:
                    self.record_smtp_failure(mx_host)
                    last_failure = f"SMTP连接被服务器关闭（{phase}阶段）"
                except socket.timeout:
                    self.record_smtp_failure(mx_host)
                    last_failure = f"SMTP连接超时（{phase}阶段）"
                except (ConnectionRefusedError, socket.gaierror) as exc:
                    self.record_smtp_failure(mx_host)
                    last_failure = f"SMTP连接失败（{phase}阶段）: {type(exc).__name__}"
                except Exception as exc:
                    self.record_smtp_failure(mx_host)
                    last_failure = f"SMTP错误（{phase}阶段）: {str(exc)[:80]}"
                finally:
                    if server is not None:
                        try:
                            server.quit()
                        except Exception:
                            pass

        # A failed SMTP conversation is not proof that a recipient does not exist.
        # Only an explicit RCPT 550 above is allowed to produce a negative verdict.
        return None, f"SMTP暂时无法确认: {last_failure or '无有效响应'}"

    def verify_email_comprehensive(self, email, process_id=0):
        """综合验证邮箱 - 保持原版本逻辑，增加QQ和Outlook修复"""
        result = {
            'email': email,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'valid': False,
            'deliverable': None,
            'checks': {
                'format': False,
                'domain': False,
                'mx': False,
                'smtp': None
            },
            'mx_records': [],
            'smtp_result': None,
            'strategy': None,
            'message': '',
            'process_id': process_id,
            'original_index': 0,
            'consumer_fix_applied': False,  # 🆕 是否应用了修复策略
            'consumer_provider': None,  # 🆕 消费者邮箱提供商
            'domain_type': 'unknown',  # 🆕 域名类型: normal/catch-all/consumer/no_mx
            'verification_method': 'standard'  # 🆕 验证方法: standard/data_command/catch-all_detected
        }

        try:
            # 第一步：格式检查
            if not self.is_valid_email_format(email):
                result['message'] = '邮箱格式不正确'
                result['deliverable'] = False
                result['checks']['smtp'] = False
                return result

            result['checks']['format'] = True

            domain = email.split('@')[1]

            # ================================================================
            # 🆕 Outlook 体系邮箱: 走微软官方接口(HTTPS), 不走 SMTP
            # 结果填入与 SMTP 完全相同的字段结构, 保证 CSV 输出趋于一致、人人看得懂。
            # ================================================================
            if is_outlook_domain(domain):
                result['strategy'] = 'outlook_http'
                result['consumer_fix_applied'] = True
                result['consumer_provider'] = 'Outlook(微软接口)'
                result['verification_method'] = 'microsoft_api'
                result['domain_type'] = 'consumer'
                # 微软接口不需要 SMTP/MX, 但为保持 CSV 各列一致, 标记基础检查通过
                result['checks']['domain'] = True
                result['checks']['mx'] = True

                exists, detail = verify_outlook_via_microsoft(email)
                result['smtp_result'] = detail  # 写入"SMTP结果码"列(人话说明)

                if exists is True:
                    result['valid'] = True
                    result['deliverable'] = True
                    result['checks']['smtp'] = True
                    result['message'] = '✅ Outlook邮箱验证通过，账号真实存在(微软接口)'
                elif exists is False:
                    result['valid'] = False
                    result['deliverable'] = False
                    result['checks']['smtp'] = False
                    result['message'] = '❌ Outlook邮箱不存在(微软接口)'
                else:
                    # 限流/分歧 -> 状态未知, 绝不误判
                    result['valid'] = True
                    result['deliverable'] = None
                    result['checks']['smtp'] = None
                    result['message'] = '⚠️ Outlook邮箱状态未知(接口限流或结果分歧)'
                return result

            strategy = self.get_domain_strategy(domain)
            result['strategy'] = strategy

            # 🆕 检查是否为需要修复的消费者邮箱
            fix_strategy = self.get_consumer_fix_strategy(domain)
            if fix_strategy:
                result['consumer_fix_applied'] = True
                result['consumer_provider'] = fix_strategy['provider']

            # 第二步：域名检查
            if not self.check_domain_exists(domain):
                result['message'] = f'域名 {domain} 不存在'
                result['smtp_result'] = '域名不存在，未发起SMTP验证'
                result['deliverable'] = False
                result['checks']['smtp'] = False
                return result

            result['checks']['domain'] = True

            # 第三步：MX记录检查
            mx_records = self.get_mx_records(domain)
            if not mx_records:
                result['message'] = f'域名 {domain} 没有邮件服务器'
                result['smtp_result'] = '未找到MX记录，未发起SMTP验证'
                result['deliverable'] = False
                result['checks']['smtp'] = False
                return result

            result['checks']['mx'] = True
            result['mx_records'] = mx_records

            # 🆕 第四步：域名类型检测 (catch-all检测)
            if fix_strategy and fix_strategy.get('strategy_type') in ('qq_aggressive', 'qq_optimized'):
                # QQ does not need a random catch-all probe. Avoid generating
                # additional recipient traffic against its protected MX hosts.
                domain_type = 'consumer'
            else:
                domain_type = self.detect_catch_all_domain(domain)
            result['domain_type'] = domain_type

            # 如果是catch-all域名，直接标记并跳过详细验证
            if domain_type == 'catch-all':
                result['valid'] = True
                result['deliverable'] = None  # 无法确定真实性
                result['verification_method'] = 'catch-all_detected'
                result['message'] = f'域名 {domain} 使用catch-all策略，无法验证邮箱真实性'
                result['checks']['smtp'] = True
                return result
            elif domain_type == 'no_mx':
                result['message'] = f'域名 {domain} MX记录检测失败'
                result['smtp_result'] = '未找到MX记录，未发起SMTP验证'
                result['deliverable'] = False
                result['checks']['smtp'] = False
                return result

            # 第五步：SMTP验证 - 🆕 优先使用修复策略
            smtp_success = None
            smtp_message = "无SMTP响应"

            max_mx_hosts = fix_strategy.get('max_mx_hosts', 2) if fix_strategy else 2
            mx_hosts_to_try = mx_records[:max_mx_hosts]
            for i, mx_host in enumerate(mx_hosts_to_try):
                if result['consumer_fix_applied']:
                    # 🆕 使用修复版SMTP检查
                    smtp_result = self.check_smtp_delivery_fixed(email, mx_host, fix_strategy)
                    if smtp_result[0] == 'continue':
                        continue  # 继续下一次尝试
                    smtp_success, smtp_message = smtp_result
                else:
                    # 使用原版SMTP检查
                    smtp_success, smtp_message = self.check_smtp_delivery(email, mx_host, strategy)

                if smtp_success is True:
                    break
                elif smtp_success is False:
                    break

                # 🔧 优化：减少MX间隔延迟
                if i < len(mx_hosts_to_try) - 1:
                    if result['consumer_fix_applied']:
                        time.sleep(fix_strategy['mx_delay'] * 0.5)  # 🔧 优化：减半
                    else:
                        strategy_delays = {'fast': 0.2, 'normal': 0.3, 'medium': 0.5, 'strict': 0.8, 'super_aggressive': 0.5}
                        time.sleep(strategy_delays.get(strategy, 0.3))

            result['checks']['smtp'] = smtp_success
            result['smtp_result'] = smtp_message

            if (
                smtp_success is None
                and fix_strategy
                and fix_strategy.get('strategy_type') in ('qq_aggressive', 'qq_optimized')
            ):
                avatar = qq_avatar_evidence(email)
                if avatar:
                    smtp_success = True
                    result['checks']['smtp'] = True
                    result['verification_method'] = 'qq_avatar'
                    result['qq_avatar_evidence'] = avatar
                    result['smtp_result'] = (
                        f"{smtp_message}；检测到非默认 QQ 头像，作为账号存在的辅助证据"
                    )

            # 综合判断 - 保持原版本逻辑
            if result['checks']['format'] and result['checks']['domain'] and result['checks']['mx']:
                if smtp_success is True:
                    result['valid'] = True
                    result['deliverable'] = True
                    if result['consumer_fix_applied']:
                        if (
                            str(result['consumer_provider']).startswith(('QQ', 'Foxmail'))
                            and result.get('verification_method') != 'qq_avatar'
                        ):
                            result['verification_method'] = 'qq_rcpt'
                        result['message'] = f'✅ {result["consumer_provider"]}邮箱验证通过(修复策略)'
                    else:
                        result['message'] = '✅ 邮箱验证通过，确认可接收邮件'
                elif smtp_success is False:
                    result['valid'] = False
                    result['deliverable'] = False
                    if result['consumer_fix_applied']:
                        result['message'] = f'❌ {result["consumer_provider"]}邮箱不存在(修复策略)'
                    else:
                        result['message'] = '❌ 邮箱不存在或无法接收邮件'
                else:
                    result['valid'] = True
                    result['deliverable'] = None
                    if result['consumer_fix_applied']:
                        result['message'] = f'⚠️ {result["consumer_provider"]}邮箱基础验证通过，SMTP状态未知(修复策略)'
                    else:
                        result['message'] = '⚠️ 邮箱基础验证通过，SMTP状态未知'

            return result

        except Exception as e:
            result['message'] = f'验证过程出错: {str(e)}'
            return result


def worker_process(process_id, email_queue, result_queue, progress_queue, shared_domain_cache=None):
    """工作进程函数 - 优化版：支持共享域名类型缓存"""
    try:
        # 创建验证器实例
        verifier = EmailVerifier()
        # 🔧 如果有共享缓存，预加载到本地缓存
        if shared_domain_cache:
            try:
                # 复制共享缓存到本地
                for domain, cache_data in dict(shared_domain_cache).items():
                    verifier.domain_type_cache[domain] = cache_data
            except Exception as e:
                pass
        
        # 🔧 重写detect_catch_all_domain方法，使其优先使用共享缓存
        original_detect = verifier.detect_catch_all_domain
        def detect_with_shared_cache(domain):
            # 先检查本地缓存
            if domain in verifier.domain_type_cache:
                cache_entry = verifier.domain_type_cache[domain]
                if datetime.now() - cache_entry['checked_at'] < timedelta(hours=1):
                    return cache_entry['type']
            
            # 再检查共享缓存
            if shared_domain_cache and domain in shared_domain_cache:
                try:
                    cache_entry = shared_domain_cache[domain]
                    if datetime.now() - cache_entry['checked_at'] < timedelta(hours=1):
                        # 同步到本地缓存
                        verifier.domain_type_cache[domain] = cache_entry
                        return cache_entry['type']
                except:
                    pass
            
            # 调用原始方法
            result = original_detect(domain)
            
            # 将结果同步到共享缓存
            if shared_domain_cache and domain in verifier.domain_type_cache:
                try:
                    shared_domain_cache[domain] = verifier.domain_type_cache[domain]
                except:
                    pass
            
            return result
        
        verifier.detect_catch_all_domain = detect_with_shared_cache
        
        processed_count = 0
        dns_cache_hits = 0
        consumer_fix_count = 0  # 🆕 修复策略应用计数

        while True:
            try:
                # 从队列获取邮箱，5秒超时
                email_data = email_queue.get(timeout=5)
                if email_data is None:  # 结束信号
                    break

                email, index = email_data
                domain = email.split('@')[1].lower()

                # 🆕 检查是否为需要修复的消费者邮箱
                is_consumer_fix = verifier.is_consumer_fix_supported(domain)
                if is_consumer_fix:
                    consumer_fix_count += 1

                # 检查是否会从DNS缓存受益
                cache_before = len(verifier.dns_cache)

                # 更新进度 - 开始处理
                progress_queue.put({
                    'process_id': process_id,
                    'processed': processed_count,
                    'current_email': email,
                    'status': 'processing',
                    'is_consumer_fix': is_consumer_fix  # 🆕 添加修复策略标识
                })

                # 验证邮箱
                result = verifier.verify_email_comprehensive(email, process_id)
                result['original_index'] = index

                # 检查DNS缓存是否被使用
                cache_after = len(verifier.dns_cache)
                if cache_after == cache_before and f"mx_{domain}" in verifier.dns_cache:
                    dns_cache_hits += 1
                    result['dns_cached'] = True
                else:
                    result['dns_cached'] = False

                # 发送结果
                result_queue.put(result)
                processed_count += 1

                # 🔧 优化：减少进程间延迟（不影响准确率，因为每个邮箱验证是独立的）
                if is_consumer_fix:
                    fix_strategy = verifier.get_consumer_fix_strategy(domain)
                    if fix_strategy:
                        time.sleep(fix_strategy['mx_delay'] * 0.3)  # 🔧 优化：大幅减少延迟
                    else:
                        time.sleep(0.2)
                else:
                    # 🔧 优化：减少延迟
                    strategy = result.get('strategy', 'normal')
                    strategy_delays = {
                        'fast': 0.1, 'normal': 0.2, 'medium': 0.3,
                        'strict': 0.5, 'super_aggressive': 0.3
                    }
                    time.sleep(strategy_delays.get(strategy, 0.2))

            except Exception as e:
                # 队列超时，检查是否还有任务
                if email_queue.empty():
                    break
                progress_queue.put({
                    'process_id': process_id,
                    'error': str(e),
                    'status': 'error'
                })

        # 进程结束时发送统计信息
        progress_queue.put({
            'process_id': process_id,
            'processed': processed_count,
            'consumer_fix_count': consumer_fix_count,  # 🆕 修复策略统计
            'dns_cache_hits': dns_cache_hits,
            'dns_cache_size': len(verifier.dns_cache),
            'status': 'completed'
        })

    except Exception as e:
        progress_queue.put({
            'process_id': process_id,
            'error': str(e),
            'status': 'failed'
        })


class EmailSender:
    """邮件发送器 - 用于发送验证结果"""

    def __init__(self):
        self.smtp_configs = {
            'gmail': {
                'server': 'smtp.gmail.com',
                'port': 587,
                'use_tls': True,
                'name': 'Gmail'
            },
            'qq': {
                'server': 'smtp.qq.com',
                'port': 587,
                'use_tls': True,
                'name': 'QQ邮箱'
            },
            '163': {
                'server': 'smtp.163.com',
                'port': 465,
                'use_tls': False,
                'use_ssl': True,
                'name': '163邮箱'
            },
            'outlook': {
                'server': 'smtp-mail.outlook.com',
                'port': 587,
                'use_tls': True,
                'name': 'Outlook'
            }
        }

    def detect_email_provider(self, email):
        """检测邮箱提供商"""
        domain = email.split('@')[1].lower()
        if 'gmail' in domain:
            return 'gmail'
        elif 'qq.com' in domain or 'foxmail' in domain:
            return 'qq'
        elif '163.com' in domain:
            return '163'
        elif 'outlook' in domain or 'hotmail' in domain or 'live' in domain:
            return 'outlook'
        return None

    def send_verification_results(self, sender_email, sender_password, recipient_email,
                                   csv_filepath, summary_text):
        """发送验证结果邮件"""
        try:
            # 检测发件人邮箱类型
            provider = self.detect_email_provider(sender_email)
            if not provider:
                return False, "不支持的邮箱类型，请使用Gmail、QQ、163或Outlook邮箱"

            config = self.smtp_configs[provider]

            # 创建邮件
            msg = MIMEMultipart()
            msg['From'] = sender_email
            msg['To'] = recipient_email
            msg['Subject'] = f'邮箱验证结果 - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'

            # 邮件正文
            body = f"""
邮箱验证结果报告
{'='*50}

{summary_text}

详细结果请查看附件中的CSV文件。

此邮件由邮箱验证工具自动发送。
发送时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
"""
            msg.attach(MIMEText(body, 'plain', 'utf-8'))

            # 添加CSV附件
            if os.path.exists(csv_filepath):
                with open(csv_filepath, 'rb') as f:
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition',
                                    f'attachment; filename={os.path.basename(csv_filepath)}')
                    msg.attach(part)

            # 发送邮件
            if config.get('use_ssl', False):
                server = smtplib.SMTP_SSL(config['server'], config['port'], timeout=30)
            else:
                server = smtplib.SMTP(config['server'], config['port'], timeout=30)
                if config.get('use_tls', False):
                    server.starttls()

            server.login(sender_email, sender_password)
            server.send_message(msg)
            server.quit()

            return True, f"邮件已成功发送到 {recipient_email}"

        except smtplib.SMTPAuthenticationError:
            return False, "邮箱认证失败，请检查邮箱地址和密码/授权码"
        except smtplib.SMTPException as e:
            return False, f"邮件发送失败: {str(e)}"
        except Exception as e:
            return False, f"发送过程出错: {str(e)}"

    def send_text_email(self, sender_email, sender_password, recipient_email,
                        subject, body):
        """🆕 发送一封纯文本邮件(无附件), 用于异步提醒(如迟到退信告警)。"""
        try:
            provider = self.detect_email_provider(sender_email)
            if not provider:
                return False, "不支持的发件邮箱类型"
            config = self.smtp_configs[provider]

            msg = MIMEMultipart()
            msg['From'] = sender_email
            msg['To'] = recipient_email
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain', 'utf-8'))

            if config.get('use_ssl', False):
                server = smtplib.SMTP_SSL(config['server'], config['port'], timeout=30)
            else:
                server = smtplib.SMTP(config['server'], config['port'], timeout=30)
                if config.get('use_tls', False):
                    server.starttls()
            server.login(sender_email, sender_password)
            server.send_message(msg)
            server.quit()
            return True, f"提醒邮件已发送到 {recipient_email}"
        except Exception as e:
            return False, f"提醒邮件发送失败: {str(e)}"


class CatchAllBounceProber:
    """🆕 Catch-all 域名"实发探针 + 退信检测"工具。

    用一个真实发件账号(默认 QQ)给 catch-all 域名下一个必定不存在的随机地址
    发一封正文为 "1" 的真实邮件, 之后登录该账号收件箱(IMAP)查退信:
      - 收到退信 = 该域名连不存在地址都会退, 即真实校验生效 -> 整域判死;
      - 未退信   = 该域名静默接收(真 catch-all) -> 整域判可投递。

    仅支持 QQ 发件账号(已在脚本中预配置)。SMTP: smtp.qq.com:465(SSL);
    IMAP: imap.qq.com:993(SSL)。其它服务商可按需扩展 _SMTP/_IMAP 映射。
    """

    _SMTP = {'qq': ('smtp.qq.com', 465, True)}      # host, port, use_ssl
    _IMAP = {'qq': ('imap.qq.com', 993)}

    # 探针地址前缀; token 唯一, 用于在退信正文里精确匹配是哪个域名
    PROBE_PREFIX = 'probe'

    def __init__(self, sender_email, sender_password):
        self.sender_email = sender_email
        self.sender_password = sender_password
        self.provider = self._detect_provider(sender_email)
        self._smtp = None
        self._imap = None
        # 记录开始检测的时间, IMAP 只看这之后的新邮件, 避免历史退信干扰
        self.started_at = time.time()

    def _detect_provider(self, email):
        d = email.split('@')[-1].lower() if '@' in email else ''
        if 'qq.com' in d or 'foxmail' in d:
            return 'qq'
        return 'qq'  # 当前仅预配置 QQ, 默认按 QQ 处理

    def make_probe(self, domain):
        """生成 (token, 探针地址)。token 含随机串+时间戳, 保证全局唯一。"""
        token = (self.PROBE_PREFIX
                 + ''.join(random.choices(string.ascii_lowercase + string.digits, k=14))
                 + str(int(time.time() * 1000) % 100000))
        return token, f"{token}@{domain}"

    def _ensure_smtp(self):
        """建立(或复用)到发件服务器的已登录 SMTP 连接。"""
        if self._smtp is not None:
            return self._smtp
        host, port, use_ssl = self._SMTP[self.provider]
        if use_ssl:
            srv = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            srv = smtplib.SMTP(host, port, timeout=30)
            srv.starttls()
        srv.login(self.sender_email, self.sender_password)
        self._smtp = srv
        return srv

    def send_probe(self, probe_addr):
        """给探针地址发一封正文为 '1' 的真实邮件。
        返回 (ok, detail): ok=True 表示发件服务器已接收(进入投递队列)。"""
        try:
            srv = self._ensure_smtp()
            msg = MIMEText('1', 'plain', 'utf-8')
            msg['From'] = self.sender_email
            msg['To'] = probe_addr
            msg['Subject'] = '1'
            # sendmail 不抛异常即视为发件方已接收; 真正的退信稍后异步回到收件箱
            srv.sendmail(self.sender_email, [probe_addr], msg.as_string())
            return True, 'sent'
        except smtplib.SMTPRecipientsRefused as e:
            # 发件服务器当场拒收该收件人(极少见), 视为发送失败, 不据此裁定
            return False, f'收件人被拒: {str(e)[:60]}'
        except smtplib.SMTPException as e:
            # 连接可能已坏, 置空以便下次重连
            self._smtp = None
            return False, f'SMTP错误: {str(e)[:60]}'
        except Exception as e:
            self._smtp = None
            return False, f'{type(e).__name__}: {str(e)[:50]}'

    def close_smtp(self):
        if self._smtp is not None:
            try:
                self._smtp.quit()
            except Exception:
                pass
            self._smtp = None

    def _ensure_imap(self):
        """建立(或复用)到收件箱的已登录 IMAP 连接, 并选中收件箱。"""
        if self._imap is not None:
            return self._imap
        host, port = self._IMAP[self.provider]
        srv = imaplib.IMAP4_SSL(host, port)
        srv.login(self.sender_email, self.sender_password)
        # QQ 的 IMAP 要求登录后发送 ID 命令, 否则可能报 "Unsafe Login"
        try:
            srv._simple_command('ID', '("name" "verifier" "version" "1.0")')
            srv._untagged_response('OK', [None], 'ID')
        except Exception:
            pass
        srv.select('INBOX')
        self._imap = srv
        return srv

    def check_bounces(self, tokens):
        """在收件箱中查找哪些 token 收到了退信。返回命中的 token 集合。

        策略: 退信(NDR/Mailer-Daemon)正文里会原样引用被退回的收件人地址,
        其中含我们埋的唯一 token。逐封扫描最近邮件正文做子串匹配即可精确归属,
        不依赖发件人是不是 mailer-daemon(不同服务商退信发件人写法不一)。"""
        if not tokens:
            return set()
        hits = set()
        try:
            srv = self._ensure_imap()
            typ, data = srv.search(None, 'ALL')
            if typ != 'OK' or not data or not data[0]:
                return set()
            ids = data[0].split()
            recent = ids[-40:] if len(ids) > 40 else ids  # 只看最近 40 封
            for mid in reversed(recent):
                try:
                    typ, msg_data = srv.fetch(mid, '(RFC822)')
                    if typ != 'OK' or not msg_data:
                        continue
                    raw = None
                    for part in msg_data:
                        if isinstance(part, tuple) and len(part) == 2:
                            raw = part[1]
                            break
                    if raw is None:
                        continue
                    text = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)
                    for tok in tokens:
                        if tok in text:
                            hits.add(tok)
                    if hits >= set(tokens):
                        break
                except Exception:
                    continue
        except Exception:
            self._imap = None
            raise
        return hits

    def close_imap(self):
        if self._imap is not None:
            try:
                self._imap.close()
            except Exception:
                pass
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None


class DistributedEmailVerifier:
    """分布式邮箱验证控制器 - 保持原版本所有功能"""

    def __init__(self):
        self.results = []
        self.process_stats = {}
        # 绝对不限制最大进程数，由用户完全控制
        self.user_max_processes = 8  # 用户设置的上限
        self.email_sender = EmailSender()  # 邮件发送器
        self.recipient_email = None  # 接收验证结果的邮箱
        # 发件账号只能通过环境变量注入，不能写入源码或提交到版本库。
        self.sender_email = os.getenv("VERIGO_SENDER_EMAIL", "")
        self.sender_password = os.getenv("VERIGO_SENDER_PASSWORD", "")

        # 🆕 Catch-all 探针专用发件账号(与结果通知发件人分开)
        self.probe_sender_email = os.getenv("VERIGO_PROBE_EMAIL", "")
        self.probe_sender_password = os.getenv("VERIGO_PROBE_PASSWORD", "")
        # 🆕 退信等待节奏:
        #   verdict_wait : 探针发出后等多久就出"初步裁定"(不阻塞导出), 默认60秒
        #   monitor_total: 后台继续监听收件箱的总时长(含上面60秒), 默认10分钟
        #   监听期内若之前判"可投递"的域名迟到退信 -> 发邮件提醒
        self.catch_all_verdict_wait = 60       # 初步裁定等待(秒)
        self.catch_all_monitor_total = 600     # 后台监听总时长(秒) = 10分钟
        # 后台探针线程的运行态(由 _start_catch_all_probes 填充)
        self._probe_state = None

    def set_max_processes(self, max_processes):
        """设置最大进程数"""
        if 1 <= max_processes <= 8:
            self.user_max_processes = max_processes
            print(f"✅ 已设置最大进程数为: {max_processes}")
            return True
        else:
            print(f"❌ 进程数必须在1-8之间")
            return False

    def load_emails_from_file(self, filepath):
        """从文件加载邮箱列表 - 完全保持原版本逻辑"""
        filepath = Path(filepath)
        emails = []

        try:
            if filepath.suffix.lower() == '.csv':
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        reader = csv.reader(f)
                        for row in reader:
                            if row:
                                for cell in row:
                                    if cell and '@' in cell and '.' in cell:
                                        emails.append(cell.strip())
                                        break
                except UnicodeDecodeError:
                    with open(filepath, 'r', encoding='gbk') as f:
                        reader = csv.reader(f)
                        for row in reader:
                            if row:
                                for cell in row:
                                    if cell and '@' in cell and '.' in cell:
                                        emails.append(cell.strip())
                                        break

            elif filepath.suffix.lower() == '.txt':
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line and '@' in line and '.' in line:
                                emails.append(line)
                except UnicodeDecodeError:
                    with open(filepath, 'r', encoding='gbk') as f:
                        for line in f:
                            line = line.strip()
                            if line and '@' in line and '.' in line:
                                emails.append(line)

            elif filepath.suffix.lower() == '.json':
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        if isinstance(data, list):
                            emails = [str(item).strip() for item in data if '@' in str(item)]
                        elif isinstance(data, dict) and 'emails' in data:
                            emails = [str(item).strip() for item in data['emails'] if '@' in str(item)]
                except UnicodeDecodeError:
                    with open(filepath, 'r', encoding='gbk') as f:
                        data = json.load(f)
                        if isinstance(data, list):
                            emails = [str(item).strip() for item in data if '@' in str(item)]
                        elif isinstance(data, dict) and 'emails' in data:
                            emails = [str(item).strip() for item in data['emails'] if '@' in str(item)]

            else:
                print(f"❌ 不支持的文件格式: {filepath.suffix}")
                return []

        except Exception as e:
            print(f"❌ 加载文件失败: {e}")
            return []

        # 🆕 不再静默删除重复邮箱: 保留全部邮箱, 重复项在导出表格中单独备注
        # (去重检测与标注逻辑见 export_to_csv)
        return emails

    def _clean_email_list(self, emails):
        """🆕 验证前清洗邮箱列表 —— 直接删除(不备注):
          1) 含空格/非法字符(strip 后仍有内部空格、非ASCII、非法符号)的邮箱
          2) 重复邮箱(以 strip 后小写为准, 保留首次出现的原始写法)
        返回清洗后的邮箱列表, 并打印删除统计。"""
        cleaned = []
        seen = set()
        removed_bad = []   # (原始邮箱, 问题说明)
        removed_dup = []   # 被去掉的重复邮箱

        for raw in emails:
            email = str(raw).strip()
            if not email:
                continue

            # 1) 空格 / 非法字符检测 —— 有问题直接剔除
            is_clean, issue = check_email_characters(email)
            if not is_clean:
                removed_bad.append((email, issue))
                continue

            # 2) 去重(大小写不敏感, 保留首次出现)
            key = email.lower()
            if key in seen:
                removed_dup.append(email)
                continue
            seen.add(key)
            cleaned.append(email)

        if removed_dup or removed_bad:
            print("🧹 验证前清洗:")
            print(f"   原始: {len(emails)} 个 → 保留: {len(cleaned)} 个")
            if removed_dup:
                print(f"   🔁 删除重复: {len(removed_dup)} 个")
                for e in removed_dup[:10]:
                    print(f"      - {e}")
                if len(removed_dup) > 10:
                    print(f"      ... 其余 {len(removed_dup) - 10} 个略")
            if removed_bad:
                print(f"   ⚠️ 删除含空格/非法字符: {len(removed_bad)} 个")
                for e, issue in removed_bad[:10]:
                    print(f"      - {repr(e)} ({issue})")
                if len(removed_bad) > 10:
                    print(f"      ... 其余 {len(removed_bad) - 10} 个略")

        return cleaned

    def verify_batch_distributed(self, emails, num_processes=None, result_callback=None, should_stop=None):
        """分布式批量验证 - 优化版：预先检测域名类型，避免重复检测"""
        if not emails:
            print("❌ 没有邮箱需要验证")
            return []

        # 🆕 验证前清洗: 直接删除重复邮箱 + 含空格/非法字符的邮箱(不再备注)
        emails = self._clean_email_list(emails)
        if not emails:
            print("❌ 清洗后没有可验证的邮箱")
            return []

        total_emails = len(emails)

        # 🆕 分析修复策略分布和提取唯一域名
        verifier_temp = EmailVerifier()
        consumer_fix_count = 0
        fix_breakdown = {}
        unique_domains = set()

        for email in emails:
            # 🔧 修复：检查邮箱格式，跳过无效邮箱
            if '@' not in email:
                continue
            parts = email.split('@')
            if len(parts) < 2 or not parts[1]:
                continue
            domain = parts[1].lower()
            unique_domains.add(domain)
            if verifier_temp.is_consumer_fix_supported(domain):
                consumer_fix_count += 1
                fix_strategy = verifier_temp.get_consumer_fix_strategy(domain)
                if fix_strategy:
                    provider = fix_strategy['provider']
                    fix_breakdown[provider] = fix_breakdown.get(provider, 0) + 1

        # 🔧 预先检测所有唯一域名的类型（避免多进程重复检测）
        print(f"🔍 预检测 {len(unique_domains)} 个唯一域名的类型...")
        domains_to_check = []
        for domain in unique_domains:
            # 跳过消费者域名和有专门策略的域名
            if domain not in verifier_temp.consumer_domains and domain not in verifier_temp.consumer_fix_strategies:
                domains_to_check.append(domain)
        
        if domains_to_check:
            print(f"   需要检测catch-all的域名: {len(domains_to_check)}个")

            # 🔧 优化：使用线程池并发检测catch-all（每个域名只需一次SMTP连接）
            catch_all_count = 0
            detected_catch_all = []  # 🆕 收集检测出的 catch-all 域名, 供探针使用
            max_workers = min(8, len(domains_to_check))  # 最多8个并发线程

            def check_single_domain(domain):
                """检测单个域名的catch-all状态"""
                return domain, verifier_temp.detect_catch_all_domain(domain)

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(check_single_domain, domain): domain for domain in domains_to_check}
                completed = 0
                for future in futures:
                    try:
                        domain, domain_type = future.result(timeout=10)  # 10秒超时
                        completed += 1
                        if domain_type == 'catch-all':
                            catch_all_count += 1
                            detected_catch_all.append(domain)  # 🆕
                            print(f"   🎯 [{completed}/{len(domains_to_check)}] {domain}: catch-all")
                    except Exception as e:
                        completed += 1
                        # 检测失败，默认为normal
                        pass

            print(f"   ✅ catch-all检测完成: {catch_all_count}个catch-all域名")

            # 🆕 catch-all 检测一完成就立即发探针并开始后台计时,
            # 让退信等待与下面的"非 catch-all 邮箱验证"并行进行。
            self._start_catch_all_probes(detected_catch_all)
        else:
            print(f"   所有域名都是消费者域名或有专门策略，跳过catch-all检测")

        # 确定进程数 - 完全保持原版本逻辑
        if num_processes is None:
            # 智能选择：每进程处理25-100个邮箱
            if total_emails <= 25:
                num_processes = 1
            elif total_emails <= 100:
                num_processes = 2
            elif total_emails <= 300:
                num_processes = 4
            else:
                num_processes = min(6, self.user_max_processes)
            print(f"🤖 自动选择进程数: {num_processes}")
        else:
            # 用户指定的进程数
            num_processes = min(num_processes, self.user_max_processes)
            print(f"🔧 使用指定进程数: {num_processes}")

        print(f"🚀 启动分布式验证 (QQ和Outlook修复版)")
        print(f"📧 总邮箱数: {total_emails}")
        print(f"📊 预计每进程处理: {math.ceil(total_emails / num_processes)} 个邮箱")
        print(f"🎯 BMW和Audi域名将使用激进RCPT TO策略")

        # 🆕 显示修复策略统计
        if consumer_fix_count > 0:
            print(f"🔧 应用修复策略的邮箱: {consumer_fix_count} ({consumer_fix_count/total_emails*100:.1f}%)")
            if fix_breakdown:
                print("📊 修复策略分布:")
                for provider, count in fix_breakdown.items():
                    print(f"   {provider}: {count}个")

        print(f"🔧 并行进程: {num_processes}")
        print("="*80)

        # 创建队列 - 完全保持原版本
        email_queue = Queue()
        result_queue = Queue()
        progress_queue = Queue()
        
        # 🔧 创建共享的域名类型缓存（使用Manager实现跨进程共享）
        manager = Manager()
        shared_domain_cache = manager.dict()
        
        # 将预检测的结果复制到共享缓存
        for domain, cache_data in verifier_temp.domain_type_cache.items():
            shared_domain_cache[domain] = cache_data

        # 将邮箱加入队列
        for i, email in enumerate(emails):
            email_queue.put((email, i))

        # 添加结束信号
        for _ in range(num_processes):
            email_queue.put(None)

        # 记录真实开始时间
        start_time = time.time()

        # 启动工作进程 - 传递共享缓存
        processes = []
        for i in range(num_processes):
            p = Process(target=worker_process,
                       args=(i+1, email_queue, result_queue, progress_queue, shared_domain_cache))
            p.start()
            processes.append(p)
            self.process_stats[i+1] = {'processed': 0, 'status': 'starting', 'current_email': '', 'consumer_fix_count': 0}

        print(f"✅ 已启动 {num_processes} 个验证进程")
        print("📊 开始监控验证进度...")

        # 监控进度 - 完全保持原版本逻辑
        results = []
        completed_processes = 0
        last_display_time = time.time()

        try:
            while completed_processes < num_processes:
                try:
                    if should_stop and should_stop():
                        for p in processes:
                            p.terminate()
                        break
                    # 检查进度更新
                    while True:
                        try:
                            progress = progress_queue.get_nowait()
                        except Empty:
                            break
                        process_id = progress['process_id']

                        if progress['status'] in ['completed', 'failed']:
                            completed_processes += 1

                        self.process_stats[process_id].update(progress)

                    # 收集结果
                    while True:
                        try:
                            result = result_queue.get_nowait()
                        except Empty:
                            break
                        results.append(result)
                        if result_callback:
                            result_callback(result)

                    # 定期显示进度 - 每15秒或每完成10个邮箱
                    current_time = time.time()
                    if (len(results) % 10 == 0 and len(results) > 0) or (current_time - last_display_time) > 15:
                        elapsed = current_time - start_time
                        self.display_progress(len(results), total_emails, elapsed)
                        last_display_time = current_time

                    time.sleep(0.25)

                except KeyboardInterrupt:
                    print("\n🛑 收到中断信号，正在停止所有进程...")
                    for p in processes:
                        p.terminate()
                    break

        finally:
            # 等待所有进程结束
            for p in processes:
                p.join(timeout=5)
                if p.is_alive():
                    p.terminate()

        # 收集剩余结果
        while True:
            try:
                result = result_queue.get_nowait()
            except Empty:
                break
            results.append(result)
            if result_callback:
                result_callback(result)

        # 按原始顺序排序
        results.sort(key=lambda x: x.get('original_index', 0))

        # 🆕 计算修复策略统计信息
        total_dns_cache_hits = 0
        total_dns_cache_size = 0
        dns_cached_results = 0
        total_consumer_fix_processed = 0

        for result in results:
            if result.get('dns_cached', False):
                dns_cached_results += 1

        # 从进程状态中收集DNS统计
        for process_stats in self.process_stats.values():
            total_dns_cache_hits += process_stats.get('dns_cache_hits', 0)
            total_dns_cache_size += process_stats.get('dns_cache_size', 0)
            total_consumer_fix_processed += process_stats.get('consumer_fix_count', 0)

        # 计算真实总时间
        total_time = time.time() - start_time
        rate = len(results) / total_time if total_time > 0 else 0

        print(f"\n🎉 分布式验证完成!")
        print(f"📊 处理邮箱: {len(results)}/{total_emails}")
        print(f"⏱️ 总耗时: {total_time:.1f}秒")
        print(f"⚡ 平均速度: {rate:.2f} 邮箱/秒")

        # 🆕 修复策略统计
        if total_consumer_fix_processed > 0:
            print(f"🔧 修复策略处理: {total_consumer_fix_processed}个")

        # DNS缓存效果统计
        if total_dns_cache_hits > 0 or dns_cached_results > 0:
            dns_hit_rate = (dns_cached_results / len(results)) * 100 if results else 0
            print(f"📋 DNS缓存命中: {dns_cached_results}/{len(results)} ({dns_hit_rate:.1f}%)")
            print(f"🚀 DNS查询优化: 节省了 {total_dns_cache_hits} 次重复查询")

        print("="*80)

        # 🆕 Catch-all 域名最终裁定: 探针在 catch-all 检测完成后就已后台发出并计时,
        # 这里只做收尾——等满剩余时间(若验证已耗时则几乎不再等)并写回结果。
        self._finalize_catch_all_probes(results)

        # 🔧 验证完成后自动保存缓存
        print("💾 自动保存域名缓存...")
        save_persistent_cache()

        self.results = results
        return results

    def _start_catch_all_probes(self, catch_all_domains):
        """🆕 catch-all 检测完成后立即调用: 发探针 + 启动后台监听线程。

        节奏:
          - 立即给每个 catch-all 域名发一封真实邮件(正文 "1")到 probe_<token>@域名;
          - 后台线程持续查收件箱, 总监听时长 self.catch_all_monitor_total(默认10分钟);
          - 主流程在 _finalize_catch_all_probes 里只等到 verdict_wait(默认60秒)就出初步
            裁定并放行导出, 不阻塞;
          - 裁定之后, 后台线程仍继续监听: 若某个"曾判可投递"的域名迟到退信, 发邮件提醒。
        """
        self._probe_state = None
        catch_all_domains = sorted(set(catch_all_domains))
        if not catch_all_domains:
            return

        print("\n" + "=" * 80)
        print(f"📨 Catch-all 实发探针: 共 {len(catch_all_domains)} 个 catch-all 域名")
        print(f"   发件账号: {self.probe_sender_email}")
        print(f"   初步裁定等待: {self.catch_all_verdict_wait} 秒 | 后台持续监听: {self.catch_all_monitor_total//60} 分钟")
        print(f"   每域名向一个必定不存在的随机地址发一封真实邮件(正文: 1)，据退信裁定整域")
        print(f"   ⏱️ 现在开始发探针并计时，与后续邮箱验证并行进行")
        print("=" * 80)

        prober = CatchAllBounceProber(self.probe_sender_email, self.probe_sender_password)

        token_to_domain = {}
        send_failed = {}
        for domain in catch_all_domains:
            token, probe_addr = prober.make_probe(domain)
            ok, detail = prober.send_probe(probe_addr)
            if ok:
                token_to_domain[token] = domain
                print(f"   ✅ 已发探针 {probe_addr}")
            else:
                send_failed[domain] = detail
                print(f"   ⚠️ 探针发送失败 {probe_addr}: {detail} (维持未知, 不裁定)")
        prober.close_smtp()

        now = time.time()
        state = {
            'prober': prober,
            'token_to_domain': token_to_domain,
            'send_failed': send_failed,
            'bounced_domains': set(),       # 所有已退信域名
            'deliverable_domains': set(),   # 初步裁定为"可投递"的域名(供迟到退信比对)
            'alerted_domains': set(),       # 已发过提醒的域名(防重复)
            'lock': threading.Lock(),
            'stop': threading.Event(),
            'verdict_made': threading.Event(),
            'started_at': now,
            'verdict_deadline': now + self.catch_all_verdict_wait,
            'monitor_deadline': now + self.catch_all_monitor_total,
            'thread': None,
            'all_domains': catch_all_domains,
        }

        if token_to_domain:
            t = threading.Thread(target=self._probe_poll_loop, args=(state,), daemon=True)
            t.start()
            state['thread'] = t
            print(f"   🧵 后台退信监听已启动 (每 15 秒查一次收件箱, 共 {self.catch_all_monitor_total//60} 分钟)")
        else:
            print(f"   ⚠️ 没有成功发出的探针，跳过退信监听")

        self._probe_state = state

    def _probe_poll_loop(self, state):
        """后台线程: 监听到 monitor_deadline 为止。
          - 裁定前(verdict 未做): 只累积退信域名, 给初步裁定用;
          - 裁定后: 若新退信的域名"曾被判可投递", 立即发邮件提醒该域名实际有问题。
        全部域名都已退信则提前结束(没什么可再等的了)。"""
        prober = state['prober']
        token_to_domain = state['token_to_domain']
        check_interval = 15
        try:
            while not state['stop'].is_set() and time.time() < state['monitor_deadline']:
                wait_s = min(check_interval, max(0.1, state['monitor_deadline'] - time.time()))
                state['stop'].wait(timeout=wait_s)
                if state['stop'].is_set():
                    break
                try:
                    hits = prober.check_bounces(set(token_to_domain.keys()))
                except Exception:
                    hits = set()

                new_bounced = []
                with state['lock']:
                    for tok in hits:
                        dom = token_to_domain.get(tok)
                        if dom and dom not in state['bounced_domains']:
                            state['bounced_domains'].add(dom)
                            new_bounced.append(dom)
                    verdict_done = state['verdict_made'].is_set()
                    deliverable = set(state['deliverable_domains'])
                    already = set(state['alerted_domains'])
                    all_bounced = len(state['bounced_domains']) >= len(token_to_domain)

                # 裁定后, 对"曾判可投递却迟到退信"的域名发提醒
                if verdict_done:
                    for dom in new_bounced:
                        if dom in deliverable and dom not in already:
                            with state['lock']:
                                state['alerted_domains'].add(dom)
                            self._send_late_bounce_alert(dom, state)

                if all_bounced:
                    break
        finally:
            try:
                prober.close_imap()
            except Exception:
                pass

    def _send_late_bounce_alert(self, domain, state):
        """🆕 迟到退信提醒: 之前判"可投递"的 catch-all 域名后来退信了, 通知接收邮箱。
        同时把内存里该域名的结果翻成"不可投递"(下次导出即为更正后的结果)。"""
        elapsed = time.time() - state['started_at']
        # 更正内存结果
        try:
            for r in self.results:
                addr = r.get('email', '')
                if '@' in addr and addr.split('@')[1].lower() == domain and r.get('domain_type') == 'catch-all':
                    r['valid'] = False
                    r['deliverable'] = False
                    r['verification_method'] = 'catch-all_late_bounce'
                    r['message'] = f'❌ catch-all 域名 {domain} 在初步裁定后迟到退信，更正为不可投递'
                    if isinstance(r.get('checks'), dict):
                        r['checks']['smtp'] = False
        except Exception:
            pass

        print(f"\n🚨 [迟到退信] 域名 {domain} 在第 {elapsed:.0f} 秒退信，之前判为可投递，现已更正！")

        # 发邮件提醒(发给"接收验证结果"的邮箱, 用结果通知发件账号)
        if not self.recipient_email:
            print(f"   ⚠️ 未配置接收邮箱，无法发送提醒邮件(仅控制台告警)")
            return
        subject = f'⚠️ 邮箱验证迟到退信提醒 - 域名 {domain} 实际有问题'
        body = (
            f"自动提醒：catch-all 域名实发探针出现迟到退信。\n"
            f"{'='*50}\n\n"
            f"问题域名: {domain}\n"
            f"该域名在初步裁定(发探针后 {self.catch_all_verdict_wait} 秒)时未退信，被判为【可投递】。\n"
            f"但在第 {elapsed:.0f} 秒收到了退信，说明该域名实际【不可投递】(无效)。\n\n"
            f"建议: 请将本次验证结果中域名为 {domain} 的邮箱视为无效/不可投递。\n"
            f"(程序内存中的结果已自动更正，如需要可重新导出 CSV。)\n\n"
            f"提醒时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        try:
            ok, msg = self.email_sender.send_text_email(
                self.sender_email, self.sender_password,
                self.recipient_email, subject, body)
            if ok:
                print(f"   📧 已发送提醒邮件到 {self.recipient_email}")
            else:
                print(f"   ⚠️ 提醒邮件发送失败: {msg}")
        except Exception as e:
            print(f"   ⚠️ 提醒邮件发送异常: {e}")

    def _finalize_catch_all_probes(self, results):
        """🆕 初步裁定收口: 只等到 verdict_deadline(默认60秒)就出结果并放行导出。
        后台监听线程不停, 继续监听到 monitor_deadline; 迟到退信由后台线程异步提醒。"""
        state = self._probe_state
        if not state:
            return

        token_to_domain = state['token_to_domain']
        send_failed = state['send_failed']
        catch_all_domains = state['all_domains']

        # 等到 verdict_deadline(60秒)。此前验证非catch-all已并行消耗掉一部分时间,
        # 所以这里通常只需补等很短或几乎不等。全部退信则不必等。
        if token_to_domain:
            while time.time() < state['verdict_deadline']:
                with state['lock']:
                    all_bounced = len(state['bounced_domains']) >= len(token_to_domain)
                if all_bounced:
                    break
                remaining = state['verdict_deadline'] - time.time()
                if remaining <= 0:
                    break
                print(f"\n⏳ Catch-all 初步裁定: 还需补等 {remaining:.0f} 秒(满 {self.catch_all_verdict_wait} 秒出结果)...")
                time.sleep(min(5, remaining))

        with state['lock']:
            bounced_domains = set(state['bounced_domains'])

        elapsed = time.time() - state['started_at']

        # 写回初步裁定结果
        verdict = {}  # 域名 -> (valid, deliverable, method, message)
        deliverable_domains = set()
        for domain in catch_all_domains:
            if domain in send_failed:
                continue  # 维持原 catch-all 未知
            if domain in bounced_domains:
                verdict[domain] = (
                    False, False, 'catch-all_bounce',
                    f'❌ catch-all 域名 {domain} 实发退信，整域判为不可投递'
                )
            elif domain in token_to_domain.values():
                verdict[domain] = (
                    True, True, 'catch-all_sent_ok',
                    f'✅ catch-all 域名 {domain} 实发后 {self.catch_all_verdict_wait} 秒内未退信，初步判为可投递(后台仍监听 {self.catch_all_monitor_total//60} 分钟)'
                )
                deliverable_domains.add(domain)

        applied = 0
        for r in results:
            email_addr = r.get('email', '')
            if '@' not in email_addr:
                continue
            dom = email_addr.split('@')[1].lower()
            if r.get('domain_type') == 'catch-all' and dom in verdict:
                valid, deliverable, method, message = verdict[dom]
                r['valid'] = valid
                r['deliverable'] = deliverable
                r['verification_method'] = method
                r['message'] = message
                if isinstance(r.get('checks'), dict):
                    r['checks']['smtp'] = deliverable
                applied += 1

        # 告诉后台线程: 裁定已完成 + 哪些域名判了可投递(用于迟到退信比对)
        with state['lock']:
            state['deliverable_domains'] = deliverable_domains
        state['verdict_made'].set()

        # 裁定小结
        print("\n" + "=" * 80)
        print(f"📊 Catch-all 初步裁定 (历时 {elapsed:.0f} 秒):")
        good = [d for d in verdict if verdict[d][0]]
        bad = [d for d in verdict if not verdict[d][0]]
        print(f"   ✅ 可投递域名: {len(good)} 个")
        for d in good:
            print(f"      - {d}")
        print(f"   ❌ 判死域名(退信): {len(bad)} 个")
        for d in bad:
            print(f"      - {d}")
        if send_failed:
            print(f"   ⚠️ 未裁定(探针发送失败, 维持未知): {len(send_failed)} 个")
            for d in send_failed:
                print(f"      - {d}: {send_failed[d]}")
        print(f"   ↺ 已更新 {applied} 条邮箱结果")
        if deliverable_domains:
            remain_min = max(0, (state['monitor_deadline'] - time.time())) / 60
            print(f"   🧵 后台将继续监听约 {remain_min:.1f} 分钟; 若上述可投递域名迟到退信会发邮件提醒")
        print("=" * 80)
        # 注意: 不清空 self._probe_state, 后台线程仍在用它继续监听

    def display_progress(self, completed, total, elapsed_time):
        """显示进度信息 - 保持原版本逻辑，增加修复策略显示"""
        if elapsed_time > 0:
            rate = completed / elapsed_time
            eta = (total - completed) / rate if rate > 0 and completed < total else 0
        else:
            rate = 0
            eta = 0

        progress_percent = (completed / total) * 100 if total > 0 else 0

        # 创建进度条
        bar_length = 50
        filled_length = int(bar_length * progress_percent / 100)
        bar = '█' * filled_length + '░' * (bar_length - filled_length)

        print(f"\n📊 验证进度: [{bar}] {progress_percent:5.1f}% ({completed}/{total})")
        print(f"⏱️ 已用时: {elapsed_time:.1f}秒 | ⚡ 当前速度: {rate:.2f}邮箱/秒")
        if eta > 0:
            print(f"🔮 预计还需: {eta:.1f}秒")

        # 显示各进程状态 - 🆕 增加修复策略标识
        print("🔄 进程状态:")
        active_processes = 0
        total_fix_processed = 0

        for pid, stats in sorted(self.process_stats.items()):
            status = stats.get('status', 'unknown')
            processed = stats.get('processed', 0)
            fix_count = stats.get('consumer_fix_count', 0)
            current = stats.get('current_email', '')
            is_consumer_fix = stats.get('is_consumer_fix', False)

            total_fix_processed += fix_count

            # 状态图标
            if status == 'completed':
                status_icon = '✅'
                status_text = '已完成'
            elif status == 'processing':
                status_icon = '🔍'
                status_text = '验证中'
                active_processes += 1
                # 🆕 如果当前处理的是修复策略邮箱，显示特殊图标
                if is_consumer_fix:
                    status_icon = '🔧'
            elif status == 'starting':
                status_icon = '🚀'
                status_text = '启动中'
                active_processes += 1
            elif status in ['error', 'failed']:
                status_icon = '❌'
                status_text = '错误'
            else:
                status_icon = '⚪'
                status_text = '未知'
                active_processes += 1

            # 显示当前处理的邮箱（截断长邮箱）
            current_short = current[:25] + '...' if len(current) > 25 else current

            # 🆕 显示修复策略处理数量
            print(f"  {status_icon} 进程{pid}: {processed:3d}个 (🔧{fix_count}) | {status_text} | {current_short}")

        print(f"💻 活跃进程: {active_processes}/{len(self.process_stats)}")
        if total_fix_processed > 0:
            print(f"🔧 已处理修复策略邮箱: {total_fix_processed}个")

    def export_to_csv(self, filename=None):
        """导出结果到CSV - 保持原版本逻辑，增加修复策略字段"""
        if not self.results:
            print("❌ 没有可导出的结果")
            return None

        if filename is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"distributed_verification_results_{timestamp}.csv"

        # 确保文件名是安全的
        import os
        filename = os.path.basename(filename)  # 防止路径注入
        if not filename.endswith('.csv'):
            filename += '.csv'

        # 🆕 增加修复策略和域名检测相关字段
        fieldnames = [
            '邮箱地址', '验证时间', '总体状态', '可投递性', '验证策略',
            '格式检查', '域名检查', 'MX记录检查', 'SMTP检查',
            'SMTP结果码', '验证消息', '处理进程', 'DNS缓存命中',
            '修复策略应用', '消费者邮箱提供商',  # 🆕 修复策略字段
            '域名类型', '验证方法'  # 🆕 域名检测字段
        ]

        try:
            print(f"📝 开始导出 {len(self.results)} 条结果到: {filename}")

            with open(filename, 'w', newline='', encoding='utf-8-sig') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()

                exported_count = 0

                for result in self.results:
                    try:
                        # 安全获取字段值
                        email = str(result.get('email', ''))
                        timestamp = str(result.get('timestamp', ''))
                        valid = result.get('valid', False)
                        deliverable = result.get('deliverable', None)
                        strategy = str(result.get('strategy', 'normal'))
                        smtp_result = str(result.get('smtp_result', ''))
                        message = str(result.get('message', ''))
                        process_id = result.get('process_id', 0)
                        dns_cached = result.get('dns_cached', False)

                        # 🆕 修复策略相关字段
                        consumer_fix_applied = result.get('consumer_fix_applied', False)
                        consumer_provider = str(result.get('consumer_provider', ''))

                        # 🆕 域名检测相关字段
                        domain_type = str(result.get('domain_type', 'unknown'))
                        verification_method = str(result.get('verification_method', 'standard'))

                        # 安全获取checks字段
                        checks = result.get('checks', {})
                        format_check = checks.get('format', False)
                        domain_check = checks.get('domain', False)
                        mx_check = checks.get('mx', False)
                        smtp_check = checks.get('smtp', None)

                        writer.writerow({
                            '邮箱地址': email,
                            '验证时间': timestamp,
                            '总体状态': '有效' if valid else '无效',
                            '可投递性': '250确认' if deliverable is True else '550拒绝' if deliverable is False else '未知',
                            '验证策略': strategy,
                            '格式检查': '✅' if format_check else '❌',
                            '域名检查': '✅' if domain_check else '❌',
                            'MX记录检查': '✅' if mx_check else '❌',
                            'SMTP检查': '✅' if smtp_check is True else '❌' if smtp_check is False else '⚠️',
                            'SMTP结果码': smtp_result,
                            '验证消息': message,
                            '处理进程': str(process_id),
                            'DNS缓存命中': '✅' if dns_cached else '❌',
                            '修复策略应用': '是' if consumer_fix_applied else '否',  # 🆕
                            '消费者邮箱提供商': consumer_provider,  # 🆕
                            '域名类型': domain_type,  # 🆕
                            '验证方法': verification_method  # 🆕
                        })
                        exported_count += 1
                    except Exception as row_error:
                        print(f"⚠️ 跳过有问题的行: {result.get('email', 'unknown')} - {row_error}")
                        continue

            print(f"✅ 验证结果已导出到: {filename}")
            print(f"📊 成功导出 {exported_count}/{len(self.results)} 条记录")
            print(f"💡 下载命令: cloudshell download {filename}")

            # 验证文件是否真的创建了
            import os
            if os.path.exists(filename):
                file_size = os.path.getsize(filename)
                print(f"📁 文件大小: {file_size} 字节")
            else:
                print("⚠️ 警告: 文件可能没有正确创建")

            return filename

        except Exception as e:
            print(f"❌ 导出失败: {e}")
            import traceback
            traceback.print_exc()
            return None

    def configure_email_notification(self):
        """配置邮件通知 - 简化版：只需输入接收者邮箱"""
        print("\n" + "="*80)
        print("📧 配置邮件通知")
        print("="*80)
        print(f"� 发件人邮箱: {self.sender_email} (已预配置)")
        print("💡 验证结果将自动发送到您指定的邮箱")
        print()

        # 只需输入接收者邮箱
        recipient = input("📮 请输入接收验证结果的邮箱地址: ").strip()
        if not recipient or '@' not in recipient:
            print("❌ 邮箱地址无效")
            return False

        # 保存配置
        self.recipient_email = recipient

        print(f"✅ 邮件通知已配置")
        print(f"   接收者: {recipient}")
        print(f"   发件人: {self.sender_email}")
        print(f"   邮箱类型: QQ邮箱")
        return True

    def send_results_email(self, csv_filepath):
        """发送验证结果邮件"""
        if not self.recipient_email or not self.sender_email or not self.sender_password:
            print("❌ 邮件通知未配置，无法发送")
            return False

        if not csv_filepath or not os.path.exists(csv_filepath):
            print("❌ CSV文件不存在，无法发送")
            return False

        print("\n📧 正在发送验证结果邮件...")

        # 生成摘要文本
        summary_text = self.get_summary_text()

        # 发送邮件
        success, message = self.email_sender.send_verification_results(
            self.sender_email,
            self.sender_password,
            self.recipient_email,
            csv_filepath,
            summary_text
        )

        if success:
            print(f"✅ {message}")
            return True
        else:
            print(f"❌ {message}")
            return False

    def get_summary_text(self):
        """生成验证结果摘要文本"""
        if not self.results:
            return "没有验证结果"

        total = len(self.results)
        valid_count = sum(1 for r in self.results if r.get('valid', False))
        deliverable_count = sum(1 for r in self.results if r.get('deliverable', False) is True)
        undeliverable_count = sum(1 for r in self.results if r.get('deliverable', False) is False)
        unknown_count = sum(1 for r in self.results if r.get('deliverable', None) is None)

        # 修复策略统计
        consumer_fix_count = sum(1 for r in self.results if r.get('consumer_fix_applied', False))

        # 域名类型统计
        catch_all_count = sum(1 for r in self.results if r.get('domain_type', '') == 'catch-all')

        summary = f"""
验证总数: {total}
有效邮箱: {valid_count} ({valid_count/total*100:.1f}%)
可投递: {deliverable_count} ({deliverable_count/total*100:.1f}%)
不可投递: {undeliverable_count} ({undeliverable_count/total*100:.1f}%)
状态未知: {unknown_count} ({unknown_count/total*100:.1f}%)
"""

        if consumer_fix_count > 0:
            summary += f"\n修复策略处理: {consumer_fix_count}个"

        if catch_all_count > 0:
            summary += f"\nCatch-all域名: {catch_all_count}个"

        return summary

    def print_summary(self):
        """打印验证结果摘要 - 保持原版本逻辑，增加修复策略统计"""
        if not self.results:
            print("❌ 没有验证结果")
            return

        total = len(self.results)
        valid = len([r for r in self.results if r['valid']])
        invalid = total - valid
        confirmed_250 = len([r for r in self.results if r['deliverable'] is True])
        rejected_550 = len([r for r in self.results if r['deliverable'] is False])
        unknown = len([r for r in self.results if r['deliverable'] is None])

        # 🆕 Catch-all统计
        catch_all = len([r for r in self.results if r.get('domain_type') == 'catch-all'])

        # 🆕 修复策略统计
        consumer_fix_applied = len([r for r in self.results if r.get('consumer_fix_applied', False)])

        # 按策略统计 - 保持原版本
        strategy_stats = defaultdict(lambda: {'total': 0, '250': 0, '550': 0})
        for result in self.results:
            strategy = result.get('strategy') or 'unknown'  # 🔧 修复：处理None值
            strategy_stats[strategy]['total'] += 1
            if result['deliverable'] is True:
                strategy_stats[strategy]['250'] += 1
            elif result['deliverable'] is False:
                strategy_stats[strategy]['550'] += 1

        # 🆕 按修复策略提供商统计
        provider_stats = defaultdict(lambda: {'total': 0, '250': 0, '550': 0})
        for result in self.results:
            if result.get('consumer_fix_applied', False):
                provider = result.get('consumer_provider', 'Unknown')
                provider_stats[provider]['total'] += 1
                if result['deliverable'] is True:
                    provider_stats[provider]['250'] += 1
                elif result['deliverable'] is False:
                    provider_stats[provider]['550'] += 1

        print("\n" + "=" * 70)
        print("📊 分布式邮箱验证结果摘要 (QQ和Outlook修复版)")
        print("=" * 70)
        print(f"📧 总计邮箱:     {total:>6}")
        print(f"✅ 有效邮箱:     {valid:>6} ({valid/total*100:>5.1f}%)")
        print(f"❌ 无效邮箱:     {invalid:>6} ({invalid/total*100:>5.1f}%)")
        print("-" * 70)
        print(f"📮 250确认:      {confirmed_250:>6} ({confirmed_250/total*100:>5.1f}%)")
        print(f"🚫 550拒绝:      {rejected_550:>6} ({rejected_550/total*100:>5.1f}%)")
        print(f"⚠️ 状态未知:     {unknown:>6} ({unknown/total*100:>5.1f}%)")
        print(f"🎯 Catch-all:   {catch_all:>6} ({catch_all/total*100:>5.1f}%)")
        print("-" * 70)

        # 🆕 修复策略统计
        if consumer_fix_applied > 0:
            print(f"🔧 修复策略应用: {consumer_fix_applied:>6} ({consumer_fix_applied/total*100:>5.1f}%)")
            print("-" * 70)
            print("📈 按修复策略提供商统计:")
            for provider, stats in sorted(provider_stats.items()):
                success_rate = (stats['250'] / stats['total'] * 100) if stats['total'] > 0 else 0
                print(f"   {provider:>12}: {stats['total']:>3}个 (250:{stats['250']:>2} 550:{stats['550']:>2} 成功率:{success_rate:>4.1f}%)")
            print("-" * 70)

        print("📈 按策略统计:")
        for strategy, stats in sorted(strategy_stats.items()):
            print(f"   {strategy:>15}: {stats['total']:>3}个 (250:{stats['250']:>2} 550:{stats['550']:>2})")
        print("=" * 70)


def main():
    """主函数 - 保持原版本所有功能"""
    print("🚀 Google Cloud Shell 全自动分布式邮箱验证工具")
    print("⚡ 智能识别域名类型，保持BMW/Audi激进策略")
    print("🔧 用户完全控制进程数，自动多进程并行处理")
    print("💾 自动结果导出和缓存保存，完全自动化")
    print("🆕 Google Cloud环境专用QQ和Outlook激进验证策略")
    print("="*80)
    print("🔥 Google Cloud优势:")
    print("   🌐 使用Google官方可信IP和域名")
    print("   📧 QQ邮箱: 使用Google域名+DATA命令二次验证")
    print("   📧 Outlook邮箱: 利用Google可信度绕过Microsoft保护")
    print("   📧 Gmail邮箱: 原生Google环境，验证准确率最高")
    print("   📧 企业邮箱: BMW/Audi等使用激进策略，准确率较高")
    print("="*80)

    # 🔧 加载持久化缓存
    load_persistent_cache()

    verifier = DistributedEmailVerifier()

    while True:
        print(f"\n⚙️ 当前设置: 最多允许 {verifier.user_max_processes} 个并行进程")
        if verifier.recipient_email:
            print(f"📧 邮件通知: 已配置 (接收者: {verifier.recipient_email})")
        else:
            print(f"📧 邮件通知: 未配置")

        print("\n📋 选择操作:")
        print("1️⃣ 手动输入邮箱进行验证")
        print("2️⃣ 从文件加载邮箱进行验证")
        print("3️⃣ 设置最大进程数 (1-8)")
        print("4️⃣ 显示上次验证结果摘要")
        print("5️⃣ 导出验证结果到CSV")
        print("6️⃣ 配置邮件通知")
        print("7️⃣ 退出程序")

        choice = input("\n请选择 (1-7): ").strip()

        if choice == '1':
            print("\n📝 请输入/粘贴邮箱地址 (每行一个):")
            print("   ⚠️ 空行会被自动跳过（避免粘贴表格时因空行提前结束）")
            print("   ✅ 全部粘贴完成后，单独输入 END （或 Ctrl+Z 回车）开始验证")
            emails = []
            while True:
                try:
                    line = input("🔹 ").strip()
                except EOFError:
                    # Ctrl+Z(Windows)/Ctrl+D(Unix) 也作为结束信号
                    break
                # 🆕 结束标记：只有显式输入 END/end 才开始验证
                if line.lower() in ('end', ':end', 'q', 'quit'):
                    break
                # 🆕 空行不再结束输入，直接跳过，继续等待后续粘贴
                if not line:
                    continue
                emails.append(line)

            if emails:
                # 询问是否配置邮件通知
                if not verifier.recipient_email:
                    email_notify = input("\n📧 是否配置邮件通知? (y/n，回车跳过): ").strip().lower()
                    if email_notify == 'y':
                        verifier.configure_email_notification()

                # 询问进程数
                process_input = input(f"\n🔧 指定进程数 (1-{verifier.user_max_processes})，回车自动选择: ").strip()
                num_processes = None
                if process_input:
                    try:
                        num_processes = int(process_input)
                        if num_processes < 1 or num_processes > verifier.user_max_processes:
                            print(f"❌ 进程数必须在1-{verifier.user_max_processes}之间，使用自动选择")
                            num_processes = None
                    except ValueError:
                        print("❌ 输入无效，使用自动选择")
                        num_processes = None

                results = verifier.verify_batch_distributed(emails, num_processes)
                verifier.print_summary()

                # 🔧 优化：自动导出CSV结果
                if results:
                    print("\n📊 自动导出验证结果...")
                    csv_file = verifier.export_to_csv()

                    # 如果配置了邮件通知，自动发送
                    if csv_file and verifier.recipient_email:
                        verifier.send_results_email(csv_file)
            else:
                print("❌ 没有输入任何邮箱地址")

        elif choice == '2':
            filepath = input("\n📁 请输入邮箱文件路径 (.txt/.csv/.json): ").strip()
            if not filepath:
                print("❌ 文件路径不能为空")
                continue

            if not os.path.exists(filepath):
                print("❌ 文件不存在")
                continue

            emails = verifier.load_emails_from_file(filepath)
            if emails:
                print(f"📖 从文件加载了 {len(emails)} 个邮箱")

                # 询问是否配置邮件通知
                if not verifier.recipient_email:
                    email_notify = input("\n📧 是否配置邮件通知? (y/n，回车跳过): ").strip().lower()
                    if email_notify == 'y':
                        verifier.configure_email_notification()

                # 询问进程数
                process_input = input(f"\n🔧 指定进程数 (1-{verifier.user_max_processes})，回车自动选择: ").strip()
                num_processes = None
                if process_input:
                    try:
                        num_processes = int(process_input)
                        if num_processes < 1 or num_processes > verifier.user_max_processes:
                            print(f"❌ 进程数必须在1-{verifier.user_max_processes}之间，使用自动选择")
                            num_processes = None
                    except ValueError:
                        print("❌ 输入无效，使用自动选择")
                        num_processes = None

                results = verifier.verify_batch_distributed(emails, num_processes)
                verifier.print_summary()

                # 🔧 优化：自动导出CSV结果
                if results:
                    print("\n📊 自动导出验证结果...")
                    csv_file = verifier.export_to_csv()

                    # 如果配置了邮件通知，自动发送
                    if csv_file and verifier.recipient_email:
                        verifier.send_results_email(csv_file)
            else:
                print("❌ 文件中没有找到有效的邮箱地址")

        elif choice == '3':
            current_max = verifier.user_max_processes
            new_max = input(f"\n🔧 当前最大进程数: {current_max}，输入新的进程数 (1-8): ").strip()

            try:
                new_max = int(new_max)
                if verifier.set_max_processes(new_max):
                    pass  # 成功消息已在方法中显示
                else:
                    pass  # 错误消息已在方法中显示
            except ValueError:
                print("❌ 请输入有效的数字")

        elif choice == '4':
            verifier.print_summary()

        elif choice == '5':
            if verifier.results:
                csv_file = verifier.export_to_csv()
                # 询问是否发送邮件
                if csv_file and verifier.recipient_email:
                    send_email = input("\n📧 是否发送验证结果到配置的邮箱? (y/n): ").strip().lower()
                    if send_email == 'y':
                        verifier.send_results_email(csv_file)
            else:
                print("❌ 没有可导出的结果，请先进行验证")

        elif choice == '6':
            verifier.configure_email_notification()

        elif choice == '7':
            # 🆕 若后台退信监听仍在运行, 提醒用户(退出会终止监听, 迟到退信将收不到)
            st = verifier._probe_state
            if st and st.get('thread') and st['thread'].is_alive() and time.time() < st.get('monitor_deadline', 0):
                remain_min = max(0, (st['monitor_deadline'] - time.time())) / 60
                print(f"\n⚠️ 后台退信监听还在运行(约剩 {remain_min:.1f} 分钟)。")
                print("   现在退出会终止监听，期间到达的迟到退信将不会再发提醒。")
                ans = input("   是否等待监听结束再退出? (y=等待 / n=直接退出): ").strip().lower()
                if ans == 'y':
                    print(f"⏳ 等待后台监听结束(最多 {remain_min:.1f} 分钟)，按 Ctrl+C 可强制退出...")
                    try:
                        st['thread'].join(timeout=max(0, st['monitor_deadline'] - time.time()) + 5)
                    except KeyboardInterrupt:
                        print("\n🛑 已强制结束等待")
            # 🔧 退出前再次保存持久化缓存（确保所有数据都已保存）
            print("💾 保存域名缓存...")
            save_persistent_cache()
            print("👋 谢谢使用，再见!")
            break

        else:
            print("❌ 无效选择，请重新输入")


if __name__ == "__main__":
    # 处理信号
    def signal_handler(signum, frame):
        print("\n🛑 程序被中断")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    main()
