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

import os

from app.core.smtp_verifier import EmailVerifier, smtp_gate_capacity
from app.core.verification_batch import VerificationBatchRunner
from app.core.verification_worker import run_verification_worker
from app.core.verification_input import VerificationInput
from app.core.verification_cli import (
    configure_email_notification,
    run_verification_cli,
    send_results_email,
)


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


from app.core.verification_reporting import VerificationReporter

class DistributedEmailVerifier:
    """分布式邮箱验证控制器 - 保持原版本所有功能"""

    def __init__(self):
        self.results = []
        self.process_stats = {}
        # 绝对不限制最大进程数，由用户完全控制
        self.user_max_processes = 8  # 用户设置的上限
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
        return configure_email_notification(self)

    def send_results_email(self, csv_filepath):
        return send_results_email(self, csv_filepath)

def main():
    """Keep the historical entry point while delegating interactive concerns."""
    run_verification_cli(DistributedEmailVerifier, load_persistent_cache, save_persistent_cache)


if __name__ == "__main__":
    main()
