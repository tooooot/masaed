"""مخزّن تتبّع خفيف (ذاكرة) لتفكيك كل رسالة إلى طبقات: الاتجاه/السياق/النية/الموظف.
يُستخدم من المنسّق (الوارد) ومن المبادرة الصادرة. للعرض في صفحة pipeline."""
from collections import deque
from datetime import datetime, timezone

_TRACE = deque(maxlen=80)


def add(direction, phone, context="", intent="", employee="", text=""):
    _TRACE.appendleft({
        "ts": datetime.now(timezone.utc).isoformat(),
        "direction": direction,                 # وارد | صادر
        "phone": str(phone),
        "context": context or "",               # L2: goal/سياق
        "intent": intent or "",                 # L3: النية
        "employee": employee or "",             # الموظف المتعامل
        "text": (text or "")[:90],
    })


def recent(n=60):
    return list(_TRACE)[:n]
