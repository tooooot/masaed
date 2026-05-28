#!/usr/bin/env python3
"""
اختبار النظام الجديد: محاكاة الأطراف + مساعد الانتقاد
"""

import sys
sys.path.insert(0, '.')

from simulator import NegotiationSimulator, CriticAssistant
import json

def test_simulator_classes():
    """اختبر الـ classes الأساسية"""
    print("=" * 60)
    print("🧪 اختبار 1: الـ Classes الأساسية")
    print("=" * 60)

    # بيانات اختبار
    seeker_data = {
        'name': 'محمد',
        'city': 'جدة',
        'district': 'النسيم',
        'rooms': 3,
        'budget': 2200000,
        'furnished': True,
        'notes': 'يفضل مفروشة'
    }

    owner_data = {
        'title': 'شقة 3 غرف',
        'city': 'جدة',
        'price': 2800000,
        'furnished': True,
        'specs': 'الجود، موقع ممتاز، قرب مدرسة',
        'terms': 'عام واحد + شيك أول'
    }

    print("\n✅ بيانات الباحث:")
    for k, v in seeker_data.items():
        print(f"   {k}: {v}")

    print("\n✅ بيانات المالك:")
    for k, v in owner_data.items():
        print(f"   {k}: {v}")

    # اختبر instantiation
    try:
        simulator = NegotiationSimulator(seeker_data, owner_data)
        print("\n✅ NegotiationSimulator instantiated بنجاح")
    except Exception as e:
        print(f"\n❌ خطأ في NegotiationSimulator: {e}")
        return False

    # اختبر Critic
    try:
        critic = CriticAssistant()
        print("✅ CriticAssistant instantiated بنجاح")
    except Exception as e:
        print(f"❌ خطأ في CriticAssistant: {e}")
        return False

    return True


def test_system_prompts():
    """اختبر أن التعليمات موجودة بشكل صحيح"""
    print("\n" + "=" * 60)
    print("🧪 اختبار 2: التعليمات (Prompts)")
    print("=" * 60)

    from simulator import _SYS_SEEKER, _SYS_OWNER, _SYS_CRITIC

    print("\n✅ Seeker Prompt:")
    print(f"   الطول: {len(_SYS_SEEKER)} حرف")
    print(f"   يحتوي على 'باحث': {('باحث' in _SYS_SEEKER)}")
    print(f"   يحتوي على تعليمات: {('السلوك' in _SYS_SEEKER or 'السلوك' in _SYS_SEEKER)}")

    print("\n✅ Owner Prompt:")
    print(f"   الطول: {len(_SYS_OWNER)} حرف")
    print(f"   يحتوي على 'مالك': {('مالك' in _SYS_OWNER)}")
    print(f"   يحتوي على تعليمات: {('السلوك' in _SYS_OWNER)}")

    print("\n✅ Critic Prompt:")
    print(f"   الطول: {len(_SYS_CRITIC)} حرف")
    print(f"   يحتوي على معايير: {('جودة' in _SYS_CRITIC)}")
    print(f"   يحتوي على JSON: {('JSON' in _SYS_CRITIC)}")

    return True


def test_api_structure():
    """اختبر أن API endpoints موجودة"""
    print("\n" + "=" * 60)
    print("🧪 اختبار 3: هيكل API")
    print("=" * 60)

    with open('scraper/scraper_api.py', 'r', encoding='utf-8') as f:
        content = f.read()

    endpoints = [
        '/api/lab/simulate',
        '/api/lab/simulate-status',
    ]

    for endpoint in endpoints:
        exists = endpoint in content
        status = "✅" if exists else "❌"
        print(f"{status} Endpoint {endpoint}: {'موجود' if exists else 'غير موجود'}")

    # اختبر functions
    functions = [
        'lab_simulate',
        'lab_simulate_status',
        'from simulator import simulate_negotiation'
    ]

    for func in functions:
        exists = func in content
        status = "✅" if exists else "❌"
        print(f"{status} Function {func}: {'موجود' if exists else 'غير موجود'}")

    return True


def test_ui_elements():
    """اختبر أن عناصر الواجهة موجودة"""
    print("\n" + "=" * 60)
    print("🧪 اختبار 4: عناصر الواجهة (UI)")
    print("=" * 60)

    with open('dashboard/lab.html', 'r', encoding='utf-8') as f:
        content = f.read()

    elements = {
        'زر المحاكاة': 'btnSimulate',
        'لوحة المحاكاة': 'simulationPanel',
        'دالة المحاكاة': 'startSimulation',
        'عرض المحادثة': 'conversationLog',
        'عرض التقييم': 'evaluationResult',
        'التوصيات': 'recommendationsBox',
        'إغلاق': 'closeSimulation',
    }

    for name, element_id in elements.items():
        exists = element_id in content
        status = "✅" if exists else "❌"
        print(f"{status} {name} ({element_id}): {'موجود' if exists else 'غير موجود'}")

    return True


def test_documentation():
    """اختبر أن التوثيق موجود"""
    print("\n" + "=" * 60)
    print("🧪 اختبار 5: التوثيق")
    print("=" * 60)

    import os

    doc_file = 'SIMULATOR_GUIDE.md'
    exists = os.path.exists(doc_file)
    status = "✅" if exists else "❌"
    print(f"{status} {doc_file}: {'موجود' if exists else 'غير موجود'}")

    if exists:
        with open(doc_file, 'r', encoding='utf-8') as f:
            content = f.read()

        sections = [
            '🎯 الفكرة الأساسية',
            '🔬 المكونات الثلاثة',
            '🚀 كيفية الاستخدام',
            '📊 مثال واقعي',
        ]

        for section in sections:
            has_section = section in content
            status = "✅" if has_section else "❌"
            print(f"{status} القسم '{section}': {'موجود' if has_section else 'غير موجود'}")

    return True


def main():
    """شغّل جميع الاختبارات"""
    print("\n")
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 10 + "🚀 اختبار نظام محاكاة الأطراف" + " " * 18 + "║")
    print("╚" + "=" * 58 + "╝")

    tests = [
        ("الـ Classes", test_simulator_classes),
        ("التعليمات", test_system_prompts),
        ("هيكل API", test_api_structure),
        ("عناصر الواجهة", test_ui_elements),
        ("التوثيق", test_documentation),
    ]

    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n❌ خطأ في اختبار {name}: {e}")
            results.append((name, False))

    # الملخص النهائي
    print("\n" + "=" * 60)
    print("📊 ملخص الاختبارات")
    print("=" * 60)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = "✅ نجح" if result else "❌ فشل"
        print(f"{status}: {name}")

    print("\n" + "-" * 60)
    print(f"النتيجة النهائية: {passed}/{total} اختبارات نجحت")

    if passed == total:
        print("\n🎉 جميع الاختبارات نجحت! النظام جاهز للاستخدام")
        return True
    else:
        print(f"\n⚠️  {total - passed} اختبار(ات) فشل")
        return False


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
