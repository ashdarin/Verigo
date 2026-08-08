"""Interactive CLI and result-notification helpers for the legacy verifier."""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Callable
from typing import Any

from app.core.result_email import EmailSender


def configure_email_notification(controller: Any, input_func: Callable[[str], str] = input) -> bool:
    """Collect the result-email recipient for a verifier controller."""
    print("\n" + "=" * 80)
    print("📧 配置邮件通知")
    print("=" * 80)
    print(f"📤 发件人邮箱: {controller.sender_email} (已预配置)")
    print("💡 验证结果将自动发送到您指定的邮箱")
    print()

    recipient = input_func("📮 请输入接收验证结果的邮箱地址: ").strip()
    if not recipient or "@" not in recipient:
        print("❌ 邮箱地址无效")
        return False

    controller.recipient_email = recipient
    print("✅ 邮件通知已配置")
    print(f"   接收者: {recipient}")
    print(f"   发件人: {controller.sender_email}")
    print("   邮箱类型: QQ邮箱")
    return True


def send_results_email(controller: Any, csv_filepath: str) -> bool:
    """Send a CSV report using the controller's environment-provided account."""
    if not controller.recipient_email or not controller.sender_email or not controller.sender_password:
        print("❌ 邮件通知未配置，无法发送")
        return False
    if not csv_filepath or not os.path.exists(csv_filepath):
        print("❌ CSV文件不存在，无法发送")
        return False

    print("\n📧 正在发送验证结果邮件...")
    success, message = EmailSender().send_verification_results(
        controller.sender_email,
        controller.sender_password,
        controller.recipient_email,
        csv_filepath,
        controller.get_summary_text(),
    )
    print(f"{'✅' if success else '❌'} {message}")
    return success


def _select_process_count(controller: Any, input_func: Callable[[str], str]) -> int | None:
    process_input = input_func(
        f"\n🔧 指定进程数 (1-{controller.user_max_processes})，回车自动选择: "
    ).strip()
    if not process_input:
        return None
    try:
        num_processes = int(process_input)
    except ValueError:
        print("❌ 输入无效，使用自动选择")
        return None
    if not 1 <= num_processes <= controller.user_max_processes:
        print(f"❌ 进程数必须在1-{controller.user_max_processes}之间，使用自动选择")
        return None
    return num_processes


def _run_verification(controller: Any, emails: list[str], input_func: Callable[[str], str]) -> None:
    if not controller.recipient_email:
        email_notify = input_func("\n📧 是否配置邮件通知? (y/n，回车跳过): ").strip().lower()
        if email_notify == "y":
            configure_email_notification(controller, input_func)

    results = controller.verify_batch_distributed(emails, _select_process_count(controller, input_func))
    controller.print_summary()
    if not results:
        return

    print("\n📊 自动导出验证结果...")
    csv_file = controller.export_to_csv()
    if csv_file and controller.recipient_email:
        send_results_email(controller, csv_file)


def _read_manual_emails(input_func: Callable[[str], str]) -> list[str]:
    print("\n📝 请输入/粘贴邮箱地址 (每行一个):")
    print("   ⚠️ 空行会被自动跳过（避免粘贴表格时因空行提前结束）")
    print("   ✅ 全部粘贴完成后，单独输入 END （或 Ctrl+Z 回车）开始验证")
    emails = []
    while True:
        try:
            line = input_func("🔹 ").strip()
        except EOFError:
            break
        if line.lower() in ("end", ":end", "q", "quit"):
            break
        if line:
            emails.append(line)
    return emails


def _print_banner() -> None:
    print("🚀 Google Cloud Shell 全自动分布式邮箱验证工具")
    print("⚡ 智能识别域名类型，保持BMW/Audi激进策略")
    print("🔧 用户完全控制进程数，自动多进程并行处理")
    print("💾 自动结果导出和缓存保存，完全自动化")
    print("🆕 Google Cloud环境专用QQ和Outlook激进验证策略")
    print("=" * 80)
    print("🔥 Google Cloud优势:")
    print("   🌐 使用Google官方可信IP和域名")
    print("   📧 QQ邮箱: 使用Google域名+DATA命令二次验证")
    print("   📧 Outlook邮箱: 利用Google可信度绕过Microsoft保护")
    print("   📧 Gmail邮箱: 原生Google环境，验证准确率最高")
    print("   📧 企业邮箱: BMW/Audi等使用激进策略，准确率较高")
    print("=" * 80)


def run_verification_cli(
    verifier_factory: Callable[[], Any],
    load_cache: Callable[[], Any],
    save_cache: Callable[[], Any],
    input_func: Callable[[str], str] = input,
) -> None:
    """Run the historical interactive verifier menu without owning verification logic."""
    _print_banner()
    load_cache()
    controller = verifier_factory()

    def signal_handler(_signum: int, _frame: Any) -> None:
        print("\n🛑 程序被中断")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while True:
        print(f"\n⚙️ 当前设置: 最多允许 {controller.user_max_processes} 个并行进程")
        if controller.recipient_email:
            print(f"📧 邮件通知: 已配置 (接收者: {controller.recipient_email})")
        else:
            print("📧 邮件通知: 未配置")
        print("\n📋 选择操作:")
        print("1️⃣ 手动输入邮箱进行验证")
        print("2️⃣ 从文件加载邮箱进行验证")
        print("3️⃣ 设置最大进程数 (1-8)")
        print("4️⃣ 显示上次验证结果摘要")
        print("5️⃣ 导出验证结果到CSV")
        print("6️⃣ 配置邮件通知")
        print("7️⃣ 退出程序")

        choice = input_func("\n请选择 (1-7): ").strip()
        if choice == "1":
            emails = _read_manual_emails(input_func)
            if emails:
                _run_verification(controller, emails, input_func)
            else:
                print("❌ 没有输入任何邮箱地址")
        elif choice == "2":
            filepath = input_func("\n📁 请输入邮箱文件路径 (.txt/.csv/.json): ").strip()
            if not filepath:
                print("❌ 文件路径不能为空")
            elif not os.path.exists(filepath):
                print("❌ 文件不存在")
            else:
                emails = controller.load_emails_from_file(filepath)
                if emails:
                    print(f"📖 从文件加载了 {len(emails)} 个邮箱")
                    _run_verification(controller, emails, input_func)
                else:
                    print("❌ 文件中没有找到有效的邮箱地址")
        elif choice == "3":
            new_max = input_func(
                f"\n🔧 当前最大进程数: {controller.user_max_processes}，输入新的进程数 (1-8): "
            ).strip()
            try:
                controller.set_max_processes(int(new_max))
            except ValueError:
                print("❌ 请输入有效的数字")
        elif choice == "4":
            controller.print_summary()
        elif choice == "5":
            if not controller.results:
                print("❌ 没有可导出的结果，请先进行验证")
                continue
            csv_file = controller.export_to_csv()
            if csv_file and controller.recipient_email:
                send_email = input_func("\n📧 是否发送验证结果到配置的邮箱? (y/n): ").strip().lower()
                if send_email == "y":
                    send_results_email(controller, csv_file)
        elif choice == "6":
            configure_email_notification(controller, input_func)
        elif choice == "7":
            print("💾 保存域名缓存...")
            save_cache()
            print("👋 谢谢使用，再见!")
            return
        else:
            print("❌ 无效选择，请重新输入")
