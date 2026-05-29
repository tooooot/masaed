#!/usr/bin/env python3
"""
محرك المحاكاة v2 — يُدخل الوسيط الحقيقي (negotiator.py) في الحلقة.

الفرق عن simulator.py القديم:
- قديماً: بوت الباحث يكلّم بوت المالك مباشرة (الوسيط غائب → تقييم "الوسيط" بلا معنى).
- الآن: الباحث ↔ الوسيط الحقيقي ↔ المالك. نختبر المنتج فعلياً.

السلامة (خط أحمر واتساب):
- نعترض negotiator.wa_send عبر thread-local: في خيط المحاكاة فقط تُلتقط كل
  الرسائل الصادرة (بما فيها إشعارات الإدارة) ولا تُرسل واتساب إطلاقاً.
- التفاوضات الحقيقية في الخيوط الأخرى لا تتأثر (تمرّ للدالة الأصلية).
- أرقام sandbox بادئة "SIM" + تنظيف صفوف DB في finally + كنس المعلّقات.

النموذج async:
- JOBS سجل في الذاكرة (thread-safe). start() يُرجع job_id فوراً ويشغّل خيطاً.
- get_status(job_id) يُرجع الحالة/المرحلة/النتيجة عند الاكتمال.
"""

import threading
import time
import uuid
from datetime import datetime, timezone

import negotiator
from bot import get_conn
from simulator import call_llm, CriticAssistant, _SYS_SEEKER, _SYS_OWNER

# ── اعتراض wa_send عبر thread-local (يُثبّت مرة واحدة) ────────────────────────────

_tls = threading.local()
_orig_wa_send = negotiator.wa_send


def _routed_wa_send(phone: str, text: str, retries: int = 3) -> bool:
    """في خيط المحاكاة: التقط الرسالة بدل إرسالها. غير ذلك: أرسل فعلياً."""
    buf = getattr(_tls, "sim_buffer", None)
    if buf is not None:
        buf.append({"to": str(phone), "text": text})
        return True
    return _orig_wa_send(phone, text, retries)


# ثبّت الاعتراض على مستوى الموديول (آمن: يفرّق بالـthread-local)
negotiator.wa_send = _routed_wa_send


# ── أدوات الـsandbox ─────────────────────────────────────────────────────────────

def _to_int(v, default=None):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def _insert_sandbox_neg(conn, reg_id, lead_phone, listing_phone, owner_data):
    """أدخل صف تفاوض sandbox مباشرة (نتجاوز شرط التسجيل في start_negotiation)."""
    negotiator.ensure_table(conn)
    price = _to_int(owner_data.get("price"))
    title = owner_data.get("title") or "عقار للإيجار"
    city = owner_data.get("city") or ""
    # نملأ listing_facts مسبقاً كي لا يحاول الوسيط تحميلها من listings الحقيقية
    facts = f"المواصفات: {owner_data.get('specs','مواصفات عادية')} | الشروط: {owner_data.get('terms','عام واحد')}"
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sanad.masaed_negotiations
                (lead_id, listing_id, lead_phone, listing_phone,
                 listing_title, listing_city, listing_price, listing_facts,
                 status, expires_at)
            VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, 'active', NOW() + INTERVAL '1 hour')
            RETURNING id
        """, (reg_id, lead_phone, listing_phone, title, city, price, facts))
        neg_id = cur.fetchone()[0]
        conn.commit()
    return neg_id


def _neg_status(conn, neg_id):
    with conn.cursor() as cur:
        cur.execute("SELECT status, agreed_price FROM sanad.masaed_negotiations WHERE id=%s", (neg_id,))
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def _cleanup_run(conn, lead_phone, listing_phone):
    """احذف صفوف هذا التشغيل فقط (آمن مع المحاكاة المتزامنة)."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM sanad.masaed_negotiations WHERE lead_phone=%s OR listing_phone=%s",
            (lead_phone, listing_phone),
        )
        cur.execute(
            "DELETE FROM sanad.masaed_contacts WHERE phone IN (%s, %s)",
            (lead_phone, listing_phone),
        )
        conn.commit()


def _sweep_orphans(conn):
    """اكنس صفوف sandbox المعلّقة من تشغيلات سابقة فشلت — المنتهية/القديمة فقط
    (لا تلمس تشغيلاً متزامناً نشطاً، صلاحيته NOW()+1h)."""
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM sanad.masaed_negotiations
            WHERE (lead_phone LIKE 'SIM%' OR listing_phone LIKE 'SIM%')
              AND (expires_at < NOW() OR created_at < NOW() - INTERVAL '1 hour')
        """)
        conn.commit()


# ── بوتات الطرفين (تردّ على الوسيط، لا على بعضهما) ──────────────────────────────

def _seeker_reply(seeker_data, mediator_to_seeker, is_first):
    furnished = "مفروشة" if seeker_data.get("furnished") else "بدون أثاث"
    system = _SYS_SEEKER.format(
        city=seeker_data.get("city", "جدة"),
        district=seeker_data.get("district") or "—",
        rooms=seeker_data.get("rooms", 3),
        budget=_to_int(seeker_data.get("budget"), 40000),
        furnished=furnished,
        special_needs=seeker_data.get("notes") or "—",
    )
    if is_first:
        prompt = "أنت تتحدث مع وسيط عقاري إلكتروني وجد طلبك. ابدأ المحادثة وعبّر عن احتياجك (جملة/جملتان):"
    else:
        relay = "\n".join(mediator_to_seeker[-3:]) or "(لا جديد)"
        prompt = f"الوسيط قال لك:\n{relay}\n\nردّك على الوسيط (جملة/جملتان):"
    return call_llm(system, prompt)


def _owner_reply(owner_data, mediator_to_owner, is_first):
    furnished = "مفروشة بالكامل" if owner_data.get("furnished") else "بدون أثاث"
    system = _SYS_OWNER.format(
        title=owner_data.get("title", "عقار"),
        city=owner_data.get("city", "جدة"),
        price=_to_int(owner_data.get("price"), 45000),
        furnished=furnished,
        specs=owner_data.get("specs", "مواصفات عادية"),
        terms=owner_data.get("terms", "عام واحد"),
    )
    relay = "\n".join(mediator_to_owner[-3:]) or "(لا جديد)"
    prompt = f"الوسيط قال لك:\n{relay}\n\nردّك على الوسيط (جملة/جملتان):"
    return call_llm(system, prompt)


# ── الحلقة الرئيسية مع الوسيط الحقيقي ────────────────────────────────────────────

MAX_ROUNDS = 5


def _run_simulation(reg_id, seeker_data, owner_data, progress_cb=None):
    """شغّل محاكاة كاملة عبر الوسيط الحقيقي. يُرجع dict جاهز للواجهة."""
    def progress(stage):
        if progress_cb:
            progress_cb(stage)

    token = uuid.uuid4().hex[:12]
    lead_phone = f"SIM{token}1"       # المستأجر (الباحث)
    listing_phone = f"SIM{token}2"    # المالك

    captured = []                     # كل ما "يرسله" الوسيط [{to, text}]
    _tls.sim_buffer = captured        # فعّل الاعتراض لهذا الخيط

    transcript = []                   # المحادثة الموحّدة الثلاثية
    seeker_inbox, owner_inbox = [], []
    drained = 0

    conn = get_conn()
    neg_id = None
    try:
        _sweep_orphans(conn)          # اكنس معلّقات قديمة فقط (لا تلمس المتزامن)
        progress("تجهيز جلسة التفاوض")
        neg_id = _insert_sandbox_neg(conn, reg_id, lead_phone, listing_phone, owner_data)

        def deliver(phone, role, text):
            """سلّم رسالة طرفٍ للوسيط الحقيقي، ثم اسحب ردود الوسيط الجديدة."""
            nonlocal drained
            transcript.append({"from": role, "text": text,
                               "timestamp": datetime.now(timezone.utc).isoformat()})
            try:
                negotiator.handle_negotiation_message(phone, text)
            except Exception as e:
                print(f"[SIM2] خطأ في الوسيط: {e}", flush=True)
            # وزّع رسائل الوسيط الجديدة على الطرفين + أضفها للمحادثة
            for item in captured[drained:]:
                who = "المستأجر" if item["to"] == lead_phone else "المالك"
                transcript.append({"from": "الوسيط", "to": who, "text": item["text"],
                                   "timestamp": datetime.now(timezone.utc).isoformat()})
                if item["to"] == lead_phone:
                    seeker_inbox.append(item["text"])
                else:
                    owner_inbox.append(item["text"])
            drained = len(captured)

        # افتتاح: الباحث يبدأ مع الوسيط
        progress("رسالة الباحث الأولى")
        msg = _seeker_reply(seeker_data, seeker_inbox, is_first=True)
        if msg:
            deliver(lead_phone, "المستأجر", msg)

        rounds = 0
        for r in range(1, MAX_ROUNDS + 1):
            rounds = r
            progress(f"جولة {r}/{MAX_ROUNDS}")

            # المالك يردّ على الوسيط
            o = _owner_reply(owner_data, owner_inbox, is_first=(r == 1))
            if o:
                deliver(listing_phone, "المالك", o)

            st, _ = _neg_status(conn, neg_id)
            if st and st != "active":
                print(f"[SIM2] أُغلق التفاوض ({st}) في الجولة {r}", flush=True)
                break

            # الباحث يردّ على الوسيط
            s = _seeker_reply(seeker_data, seeker_inbox, is_first=False)
            if s:
                deliver(lead_phone, "المستأجر", s)

            st, _ = _neg_status(conn, neg_id)
            if st and st != "active":
                print(f"[SIM2] أُغلق التفاوض ({st}) في الجولة {r}", flush=True)
                break

            time.sleep(0.2)

        # حالة التفاوض النهائية + ما فعله الوسيط
        final_status, agreed_price = _neg_status(conn, neg_id)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT lead_max_price, owner_min_price, proposed_price, needs_admin, admin_reason
                FROM sanad.masaed_negotiations WHERE id=%s
            """, (neg_id,))
            row = cur.fetchone()
        mediator_state = {}
        if row:
            mediator_state = {
                "lead_max_price": row[0], "owner_min_price": row[1],
                "proposed_price": row[2], "needs_admin": row[3], "admin_reason": row[4],
            }

        # كشف التشغيلات المُنحطّة
        if len(transcript) < 2:
            return {
                "ok": False,
                "error": "تعذّر توليد المحادثة — لا استجابة من نموذج اللغة (تحقّق من المفاتيح أو أعد المحاولة)",
                "simulation": {"messages": transcript, "rounds": rounds},
            }

        # التقييم بالناقد (الآن الوسيط الحقيقي حاضر → تقييم ذو معنى)
        progress("تقييم الأداء")
        critic = CriticAssistant()
        evaluation = critic.evaluate(transcript, seeker_data, owner_data)

        progress("اكتملت")
        return {
            "ok": True,
            "reg_id": reg_id,
            "simulation": {
                "messages": transcript,
                "rounds": rounds,
                "final_status": final_status,
                "agreed_price": agreed_price,
                "mediator_state": mediator_state,
            },
            "evaluation": evaluation,
            "recommendations": critic.get_recommendations(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        # نظافة مضمونة: عطّل الاعتراض + احذف صفوف الـsandbox
        _tls.sim_buffer = None
        try:
            _cleanup_run(conn, lead_phone, listing_phone)
        except Exception as e:
            print(f"[SIM2] فشل التنظيف: {e}", flush=True)
        try:
            conn.close()
        except Exception:
            pass


# ── سجل الـJobs (async) ──────────────────────────────────────────────────────────

JOBS = {}            # job_id -> {status, stage, result, error, started}
_jobs_lock = threading.Lock()
_MAX_JOBS = 50       # احتفظ بآخر N فقط

# ── Rate limiting (حماية من DoS التكلفة: كل تشغيل ~15 استدعاء LLM مدفوع) ──────────
_MAX_CONCURRENT = 2          # محاكاتان متزامنتان كحد أقصى
_WINDOW_SEC = 60            # نافذة زمنية
_MAX_PER_WINDOW = 6         # حد التشغيلات لكل نافذة
_starts = []               # طوابع زمنية لآخر عمليات البدء


class RateLimited(Exception):
    pass


def _set_job(job_id, **fields):
    with _jobs_lock:
        job = JOBS.setdefault(job_id, {})
        job.update(fields)


def _prune_jobs():
    with _jobs_lock:
        if len(JOBS) > _MAX_JOBS:
            for k in sorted(JOBS, key=lambda j: JOBS[j].get("started", 0))[:len(JOBS) - _MAX_JOBS]:
                JOBS.pop(k, None)


def _worker(job_id, reg_id, seeker_data, owner_data):
    def cb(stage):
        _set_job(job_id, stage=stage)
    try:
        result = _run_simulation(reg_id, seeker_data, owner_data, progress_cb=cb)
        if result.get("ok"):
            _set_job(job_id, status="done", result=result)
        else:
            _set_job(job_id, status="error", error=result.get("error", "فشلت المحاكاة"), result=result)
    except Exception as e:
        print(f"[SIM2] خطأ غير متوقع في الـjob: {e}", flush=True)
        _set_job(job_id, status="error", error=str(e))


def _check_rate_limit():
    """يرفع RateLimited عند تجاوز التزامن أو نافذة المعدّل."""
    now = time.time()
    with _jobs_lock:
        running = sum(1 for j in JOBS.values() if j.get("status") == "running")
        if running >= _MAX_CONCURRENT:
            raise RateLimited(f"محاكاة أخرى قيد التشغيل ({running}) — انتظر حتى تكتمل")
        # نظّف الطوابع خارج النافذة
        global _starts
        _starts = [t for t in _starts if now - t < _WINDOW_SEC]
        if len(_starts) >= _MAX_PER_WINDOW:
            raise RateLimited(f"تجاوزت حد {_MAX_PER_WINDOW} محاكاة/دقيقة — حاول بعد قليل")
        _starts.append(now)


def start_job(reg_id, seeker_data, owner_data):
    """ابدأ محاكاة في الخلفية وأرجع job_id فوراً."""
    _check_rate_limit()
    job_id = uuid.uuid4().hex[:16]
    _set_job(job_id, status="running", stage="بدء", result=None, error=None, started=time.time())
    _prune_jobs()
    t = threading.Thread(target=_worker, args=(job_id, reg_id, seeker_data, owner_data), daemon=True)
    t.start()
    return job_id


def get_status(job_id):
    with _jobs_lock:
        job = JOBS.get(job_id)
        return dict(job) if job else None
