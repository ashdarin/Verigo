from __future__ import annotations

import os
import smtplib
from datetime import datetime
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
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
