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

from app.core.domain_type_cache import (
    get_shared_domain_type,
    has_catch_all_evidence,
    load_persistent_cache,
    save_persistent_cache,
    set_shared_domain_type,
)

import csv
import sys
import time
import json
import os
import math
import signal
from datetime import datetime, timedelta
from pathlib import Path
from multiprocessing import Process, Queue, Manager, cpu_count
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from queue import Empty

from app.core.smtp_verifier import (
    EmailVerifier,
    check_email_characters,
    smtp_gate_capacity,
)
def worker_process(process_id, email_queue, result_queue, progress_queue, shared_domain_cache=None, shared_domain_lock=None):
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
                if (
                    datetime.now() - cache_entry['checked_at'] < timedelta(hours=1)
                    and has_catch_all_evidence(cache_entry)
                ):
                    return cache_entry['type']

            # Only one process probes a newly seen domain. The other workers
            # reuse its result rather than multiplying a three-probe check by
            # the job's concurrency.
            is_probe_owner = False
            if shared_domain_cache and shared_domain_lock:
                try:
                    with shared_domain_lock:
                        cache_entry = shared_domain_cache.get(domain)
                        if cache_entry and cache_entry.get('type') != 'probing':
                            if (
                                datetime.now() - cache_entry['checked_at'] < timedelta(hours=1)
                                and has_catch_all_evidence(cache_entry)
                            ):
                                verifier.domain_type_cache[domain] = cache_entry
                                return cache_entry['type']
                        if not cache_entry or cache_entry.get('type') != 'probing':
                            shared_domain_cache[domain] = {
                                'type': 'probing', 'checked_at': datetime.now()
                            }
                            is_probe_owner = True
                except Exception:
                    pass

            if shared_domain_cache and not is_probe_owner:
                # A concurrent worker is running the three probes. Waiting is
                # bounded; a failed owner falls back to this worker's check.
                for _ in range(20):
                    time.sleep(0.25)
                    try:
                        cache_entry = shared_domain_cache.get(domain)
                        if (
                            cache_entry and cache_entry.get('type') != 'probing'
                            and has_catch_all_evidence(cache_entry)
                        ):
                            verifier.domain_type_cache[domain] = cache_entry
                            return cache_entry['type']
                    except Exception:
                        break

            result = original_detect(domain)
            if shared_domain_cache and domain in verifier.domain_type_cache:
                try:
                    shared_domain_cache[domain] = verifier.domain_type_cache[domain]
                except Exception:
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


from app.core.result_email import EmailSender

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
                            print(f"   🎯 [{completed}/{len(domains_to_check)}] {domain}: catch-all")
                    except Exception as e:
                        completed += 1
                        # 检测失败，默认为normal
                        pass

            print(f"   ✅ catch-all检测完成: {catch_all_count}个catch-all域名")

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
        shared_domain_lock = manager.RLock()

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
                       args=(i+1, email_queue, result_queue, progress_queue, shared_domain_cache, shared_domain_lock))
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

        # 🔧 验证完成后自动保存缓存
        print("💾 自动保存域名缓存...")
        save_persistent_cache()

        self.results = results
        return results

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
