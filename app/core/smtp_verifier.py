from __future__ import annotations

import json
import math
import os
import re
import secrets
import smtplib
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta

import dns.exception
import dns.resolver

from app.config import settings
from app.core.qq_evidence import qq_avatar_evidence
from app.core.smtp_limiter import SMTPDeliveryLimiter
from app.core.verification_outcome import RETRY_DELAYED, RETRY_NEVER, apply_outcome
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
from app.core.outlook_verifier import (
    OUTLOOK_DOMAIN_PREFIXES,
    OUTLOOK_HTTP_DOMAINS,
    is_outlook_domain,
    verify_outlook_via_microsoft,
)
from app.core.domain_type_cache import (
    get_shared_domain_type,
    has_catch_all_evidence,
    load_persistent_cache,
    save_persistent_cache,
    set_shared_domain_type,
)
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
        # Normal domains are scheduled before this legacy verifier is invoked.
        # The shared job scheduler owns both capacity and backoff, so a second
        # process-local exponential limiter would fight that feedback loop.
        yield True

    def record_smtp_response(self, mx_host, code):
        host = str(mx_host).lower().rstrip('.')
        if not (host.endswith('.qq.com') or host.endswith('.foxmail.com')):
            return
        if 200 <= code < 400:
            self.smtp_limiter.record_success(mx_host)
            self.smtp_limiter.record_success('qq-smtp-global')

    def record_smtp_failure(self, mx_host):
        host = str(mx_host).lower().rstrip('.')
        if host.endswith('.qq.com') or host.endswith('.foxmail.com'):
            self.record_qq_policy_failure(mx_host)

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

    def check_domain_status(self, domain):
        """Return exists, nxdomain, or transient without collapsing DNS failures."""
        cache_key = f"domain_status_{domain}"
        with self.dns_cache_lock:
            cached = self.dns_cache.get(cache_key)
            if cached and datetime.now() - cached[0] < self.dns_cache_ttl:
                return cached[1]

        saw_authoritative_empty = False
        saw_transient_failure = False
        status = "transient"
        for record_type in ('MX', 'A', 'AAAA', 'NS', 'SOA'):
            try:
                answers = dns.resolver.resolve(domain, record_type)
                if answers:
                    status = "exists"
                    break
                saw_authoritative_empty = True
            except dns.resolver.NXDOMAIN:
                status = "nxdomain"
                break
            except dns.resolver.NoAnswer:
                saw_authoritative_empty = True
            except (dns.resolver.NoNameservers, dns.exception.DNSException):
                saw_transient_failure = True
        else:
            if saw_authoritative_empty:
                status = "exists"
            elif saw_transient_failure:
                status = "transient"

        with self.dns_cache_lock:
            self.dns_cache[cache_key] = (datetime.now(), status)
        return status

    def check_domain_exists(self, domain):
        """Backward-compatible boolean view for callers outside the result pipeline."""
        return self.check_domain_status(domain) == "exists"

    def get_mx_record_status(self, domain):
        """Return found, missing, nxdomain, or transient with MX hosts."""
        cache_key = f"mx_status_{domain}"
        with self.dns_cache_lock:
            cached = self.dns_cache.get(cache_key)
            if cached and datetime.now() - cached[0] < self.dns_cache_ttl:
                return cached[1]

        try:
            records = [
                (rdata.preference, str(rdata.exchange).rstrip('.'))
                for rdata in dns.resolver.resolve(domain, 'MX')
            ]
        except dns.resolver.NXDOMAIN:
            outcome = ("nxdomain", [])
        except dns.resolver.NoAnswer:
            outcome = ("missing", [])
        except (dns.resolver.NoNameservers, dns.exception.DNSException):
            outcome = ("transient", [])
        else:
            records.sort(key=lambda item: item[0])
            outcome = ("found", [host for _, host in records])

        with self.dns_cache_lock:
            self.dns_cache[cache_key] = (datetime.now(), outcome)
        return outcome

    def get_mx_records(self, domain):
        """Backward-compatible MX list for helpers that do not need failure semantics."""
        status, records = self.get_mx_record_status(domain)
        return records if status == "found" else []

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
                    if 500 <= code < 600:
                        return False, f"{code} {config['provider']}邮箱服务器永久拒绝"
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
        return 400 <= code < 500

    def _handle_qq_response(self, code, response, config, attempt):
        """QQ邮箱响应处理：只依据 RCPT TO，不进入 DATA 阶段。"""
        response_text = response.decode('utf-8', 'replace') if isinstance(response, bytes) else str(response)
        if code == 250:
            return True, f"250 {config['provider']} RCPT已接受: {response_text[:160]}"
        elif 500 <= code < 600:
            return False, f"{code} {config['provider']}邮箱服务器永久拒绝: {response_text[:160]}"
        elif code in [451, 452, 421]:
            # 临时失败，继续重试
            if attempt < config['max_attempts'] - 1:
                return 'continue', f"{code} {config['provider']}临时失败，重试"
            else:
                return None, f"{code} {config['provider']}临时失败，暂时无法确认"
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
        # 🔧 优先检查本地缓存
        if domain in self.domain_type_cache:
            cache_entry = self.domain_type_cache[domain]
            if (
                datetime.now() - cache_entry['checked_at'] < timedelta(hours=1)
                and has_catch_all_evidence(cache_entry)
            ):
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

            # Use the first MX consistently. A single accepted random address
            # can be an anti-enumeration response, so catch-all requires three
            # separate high-entropy probes to be accepted.
            mx_host = mx_records[0]
            probe_codes = []
            for _ in range(3):
                # The address has 128 bits of randomness and uses a fresh SMTP
                # connection, so a real mailbox collision is not plausible.
                test_email = f"probe-{secrets.token_hex(16)}@{domain}"
                server = None
                try:
                    server = smtplib.SMTP(timeout=5)
                    code, _ = server.connect(mx_host, 25)
                    if code != 220:
                        break
                    code, _ = server.ehlo(SMTP_HELO_HOST)
                    if code != 250:
                        break
                    code, _ = server.mail(SMTP_MAIL_FROM)
                    if code != 250:
                        break
                    code, _ = server.rcpt(test_email)
                    probe_codes.append(code)
                    if code != 250:
                        break
                except Exception:
                    break
                finally:
                    if server is not None:
                        try:
                            server.quit()
                        except Exception:
                            pass

            domain_type = 'catch-all' if probe_codes == [250, 250, 250] else 'normal'
            self.domain_type_cache[domain] = {
                'type': domain_type,
                'checked_at': datetime.now(),
                'probe_count': len(probe_codes),
                'probe_codes': probe_codes,
            }
            set_shared_domain_type(domain, domain_type, probe_codes=probe_codes)
            return domain_type

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
                    if 500 <= code < 600:
                        return False, f"{code} 邮箱服务器永久拒绝"
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

        # Connection failures are inconclusive. Explicit RCPT 5xx replies are
        # permanent failures and return before this point.
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
                apply_outcome(
                    result, stage='format', reason='format_invalid', retry_policy=RETRY_NEVER
                )
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
                result['consumer_provider'] = 'Outlook'
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
                    result['message'] = '✅ Outlook 邮箱已确认可投递'
                elif exists is False:
                    result['valid'] = False
                    result['deliverable'] = False
                    result['checks']['smtp'] = False
                    result['message'] = '❌ Outlook 邮箱不可投递'
                    apply_outcome(
                        result, stage='provider_api', reason='smtp_permanent', retry_policy=RETRY_NEVER
                    )
                else:
                    # 限流/分歧 -> 状态未知, 绝不误判
                    result['valid'] = True
                    result['deliverable'] = None
                    result['checks']['smtp'] = None
                    result['message'] = '⚠️ Outlook 邮箱暂时无法确认'
                    apply_outcome(
                        result, stage='provider_api', reason='provider_unknown', retry_policy=RETRY_NEVER
                    )
                return result

            strategy = self.get_domain_strategy(domain)
            result['strategy'] = strategy

            # 🆕 检查是否为需要修复的消费者邮箱
            fix_strategy = self.get_consumer_fix_strategy(domain)
            if fix_strategy:
                result['consumer_fix_applied'] = True
                result['consumer_provider'] = fix_strategy['provider']

            # 第二步：域名检查
            domain_status = self.check_domain_status(domain)
            if domain_status == 'nxdomain':
                result['message'] = f'域名 {domain} 不存在'
                result['smtp_result'] = '域名不存在，未发起SMTP验证'
                result['deliverable'] = False
                result['checks']['smtp'] = False
                apply_outcome(
                    result, stage='dns', reason='domain_nxdomain', retry_policy=RETRY_NEVER
                )
                return result
            if domain_status == 'transient':
                result['message'] = f'域名 {domain} 的 DNS 暂时无法确认'
                result['smtp_result'] = 'DNS 查询暂时失败，未发起SMTP验证'
                result['deliverable'] = None
                result['checks']['domain'] = None
                result['checks']['mx'] = None
                result['checks']['smtp'] = None
                apply_outcome(
                    result, stage='dns', reason='dns_transient', retry_policy=RETRY_DELAYED
                )
                return result

            result['checks']['domain'] = True

            # 第三步：MX记录检查
            mx_status, mx_records = self.get_mx_record_status(domain)
            if mx_status == 'nxdomain':
                result['message'] = f'域名 {domain} 不存在'
                result['smtp_result'] = '域名不存在，未发起SMTP验证'
                result['deliverable'] = False
                result['checks']['domain'] = False
                result['checks']['smtp'] = False
                apply_outcome(
                    result, stage='dns', reason='domain_nxdomain', retry_policy=RETRY_NEVER
                )
                return result
            if mx_status == 'transient':
                result['message'] = f'域名 {domain} 的 MX 暂时无法查询'
                result['smtp_result'] = 'MX 查询暂时失败，未发起SMTP验证'
                result['deliverable'] = None
                result['checks']['mx'] = None
                result['checks']['smtp'] = None
                apply_outcome(
                    result, stage='mx', reason='dns_transient', retry_policy=RETRY_DELAYED
                )
                return result
            if mx_status == 'missing':
                result['message'] = f'域名 {domain} 没有邮件服务器'
                result['smtp_result'] = '未找到MX记录，未发起SMTP验证'
                result['deliverable'] = False
                result['checks']['smtp'] = False
                apply_outcome(
                    result, stage='mx', reason='mx_missing', retry_policy=RETRY_NEVER
                )
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
                apply_outcome(
                    result, stage='mx', reason='mx_missing', retry_policy=RETRY_NEVER
                )
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
            if smtp_success is None:
                apply_outcome(
                    result, stage='smtp', reason='smtp_temporary', retry_policy=RETRY_DELAYED
                )
            elif smtp_success is False:
                apply_outcome(
                    result, stage='smtp', reason='smtp_permanent', retry_policy=RETRY_NEVER
                )
            else:
                apply_outcome(
                    result, stage='smtp', reason='smtp_accepted', retry_policy=RETRY_NEVER
                )

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
                    apply_outcome(
                        result, stage='smtp', reason='smtp_accepted', retry_policy=RETRY_NEVER
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
