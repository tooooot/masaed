#!/usr/bin/env python3
"""🔍 مساعد الطلبات والمطابقة — منظومة المصدر/الترشيح/المبادرة ككتلة متكاملة.
تفاصيلها في goals.py + haraj_scraper.py."""
from goals import (recommend_offer, run_outbound_for_seeker,
                   run_outbound_for_phone, run_periodic_rematch, session_goal)


class Sourcing:
    recommend   = staticmethod(recommend_offer)         # يرشّح أفضل عرض غير مُجرَّب
    pursue      = staticmethod(run_outbound_for_phone)  # يبادر لطلب باحث واحد
    engine      = staticmethod(run_outbound_for_seeker)
    rematch_all = staticmethod(run_periodic_rematch)    # المتابعة الدورية (الحلقة)
    goal        = staticmethod(session_goal)            # موجّه الأهداف
