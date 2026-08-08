"""Batch orchestration and console progress for the legacy verifier facade."""

from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Manager, Process, Queue
from queue import Empty

from app.core.domain_type_cache import save_persistent_cache
from app.core.smtp_verifier import EmailVerifier
from app.core.verification_worker import run_verification_worker


class VerificationBatchRunner:
    """Run the historical multiprocessing workflow against a facade controller."""

    def __init__(self, controller):
        self.controller = controller

    def run(self, emails, num_processes=None, result_callback=None, should_stop=None):
        controller = self.controller
        """分布式批量验证 - 优化版：预先检测域名类型，避免重复检测"""
        if not emails:
            print("❌ 没有邮箱需要验证")
            return []

        # 🆕 验证前清洗: 直接删除重复邮箱 + 含空格/非法字符的邮箱(不再备注)
        emails = controller._clean_email_list(emails)
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
                num_processes = min(6, controller.user_max_processes)
            print(f"🤖 自动选择进程数: {num_processes}")
        else:
            # 用户指定的进程数
            num_processes = min(num_processes, controller.user_max_processes)
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
            p = Process(
                target=run_verification_worker,
                args=(
                    i + 1,
                    email_queue,
                    result_queue,
                    progress_queue,
                    shared_domain_cache,
                    shared_domain_lock,
                    EmailVerifier,
                ),
            )
            p.start()
            processes.append(p)
            controller.process_stats[i+1] = {'processed': 0, 'status': 'starting', 'current_email': '', 'consumer_fix_count': 0}

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

                        controller.process_stats[process_id].update(progress)

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
        for process_stats in controller.process_stats.values():
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

        controller.results = results
        return results



    def display_progress(self, completed, total, elapsed_time):
        controller = self.controller
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

        for pid, stats in sorted(controller.process_stats.items()):
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

        print(f"💻 活跃进程: {active_processes}/{len(controller.process_stats)}")
        if total_fix_processed > 0:
            print(f"🔧 已处理修复策略邮箱: {total_fix_processed}个")


