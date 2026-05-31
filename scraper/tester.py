#!/usr/bin/env python3
"""🧪 مساعد المختبر — المحاكاة ووضع الاختبار (معزول، لا واتساب حقيقي).
تفاصيله في sim_engine.py + goals.start_test_negotiation."""
from sim_engine import start_job, get_status
from goals import start_test_negotiation


class Tester:
    simulate       = staticmethod(start_job)             # محاكاة async (job)
    status         = staticmethod(get_status)
    run_on_request = staticmethod(start_test_negotiation) # اختبار محاكاة على طلب حقيقي
