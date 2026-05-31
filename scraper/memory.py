#!/usr/bin/env python3
"""🧠 مساعد الحافظ — الواجهة الموحّدة للذاكرة وملفات العملاء.
المنسّق يناديه ككتلة واحدة؛ تفاصيله الداخلية في bot.py (masaed_contacts)."""
from bot import (get_contact, update_contact, build_party_profile,
                 build_memory_context, get_contact_registrations,
                 sync_contact_after_reg, get_config, set_config)


class Memory:
    touch         = staticmethod(get_contact)              # upsert + last_seen + يرجّع الملف
    profile       = staticmethod(build_party_profile)       # ملف مضغوط (حقائق)
    context       = staticmethod(build_memory_context)      # سياق نصّي للنموذج
    remember      = staticmethod(update_contact)            # تحديث بيانات
    registrations = staticmethod(get_contact_registrations) # تسجيلاته
    after_reg     = staticmethod(sync_contact_after_reg)
    get_config    = staticmethod(get_config)
    set_config    = staticmethod(set_config)
