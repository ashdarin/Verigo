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

import sys
import time
import os
import math
import signal
from multiprocessing import Process, Queue, Manager
from concurrent.futures import ThreadPoolExecutor
from queue import Empty

from app.core.smtp_verifier import EmailVerifier, smtp_gate_capacity
from app.core.verification_batch import VerificationBatchRunner
from app.core.verification_worker import run_verification_worker
from app.core.verification_input import VerificationInput


def worker_process(
    process_id,
    email_queue,
    result_queue,
    progress_queue,
    shared_domain_cache=None,
    shared_domain_lock=None,
):
    """Keep the historical multiprocessing target while delegating worker logic."""
    return run_verification_worker(
        process_id,
        email_queue,
        result_queue,
        progress_queue,
        shared_domain_cache,
        shared_domain_lock,
        EmailVerifier,
    )


from app.core.result_email import EmailSender
from app.core.verification_reporting import VerificationReporter

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
        return VerificationInput.load_emails_from_file(filepath)

    def _clean_email_list(self, emails):
        return VerificationInput.clean_email_list(emails)

    def verify_batch_distributed(self, emails, num_processes=None, result_callback=None, should_stop=None):
        return VerificationBatchRunner(self).run(
            emails, num_processes=num_processes, result_callback=result_callback,
            should_stop=should_stop,
        )

    def display_progress(self, completed, total, elapsed_time):
        return VerificationBatchRunner(self).display_progress(completed, total, elapsed_time)

    def export_to_csv(self, filename=None):
        return VerificationReporter(self.results).export_to_csv(filename)
    def get_summary_text(self):
        return VerificationReporter(self.results).get_summary_text()

    def print_summary(self):
        return VerificationReporter(self.results).print_summary()
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
