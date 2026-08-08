from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
_global_domain_type_cache = {}
_global_domain_type_cache_lock = threading.Lock()

# 🔧 持久化缓存文件路径
DOMAIN_CACHE_FILE = os.getenv("VERIGO_DOMAIN_CACHE_PATH", "domain_type_cache.json")
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
                                'checked_at': checked_at,
                                'probe_count': int(entry.get('probe_count', 0)),
                                'probe_codes': list(entry.get('probe_codes', [])),
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
                    'checked_at': entry['checked_at'].isoformat(),
                    'probe_count': int(entry.get('probe_count', 0)),
                    'probe_codes': list(entry.get('probe_codes', [])),
                }
            with open(DOMAIN_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存缓存文件失败: {e}")

def has_catch_all_evidence(cache_entry):
    """A cached Catch-all verdict is usable only with its full probe record."""
    if cache_entry.get('type') != 'catch-all':
        return True
    codes = cache_entry.get('probe_codes', [])
    return cache_entry.get('probe_count') == 3 and codes == [250, 250, 250]


def get_shared_domain_type(domain):
    """获取共享的域名类型缓存"""
    with _global_domain_type_cache_lock:
        if domain in _global_domain_type_cache:
            cache_entry = _global_domain_type_cache[domain]
            if (
                datetime.now() - cache_entry['checked_at'] < timedelta(days=DOMAIN_CACHE_TTL_DAYS)
                and has_catch_all_evidence(cache_entry)
            ):
                return cache_entry['type']
    return None

def set_shared_domain_type(domain, domain_type, *, probe_codes=None):
    """设置共享的域名类型缓存"""
    with _global_domain_type_cache_lock:
        _global_domain_type_cache[domain] = {
            'type': domain_type,
            'checked_at': datetime.now(),
            'probe_count': len(probe_codes or []),
            'probe_codes': list(probe_codes or []),
        }

# ============================================================================
# 🆕 邮箱字符检测 —— 检测空格 / 非法字符 (用于在验证前直接剔除问题邮箱)
# ----------------------------------------------------------------------------
# 合法邮箱本地部分+域名允许的字符集: 字母、数字、. _ % + - 以及分隔符 @
# 任何空白字符(空格/制表/换行)或上述集合之外的字符都视为"非法字符"。
# ============================================================================
