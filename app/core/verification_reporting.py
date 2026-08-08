"""CSV export and result summaries for verification batches."""

from __future__ import annotations

import csv
import os
import traceback
from collections import defaultdict
from datetime import datetime


class VerificationReporter:
    def __init__(self, results):
        self.results = results
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
