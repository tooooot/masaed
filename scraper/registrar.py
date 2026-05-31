#!/usr/bin/env python3
"""📝 مساعد المسجّل والمعدّل — جمع/تعديل/عرض بيانات العملاء ككتلة واحدة.
يدمج التسجيل (bot.py) والتعديل (editor.py) تحت موظف واحد."""
from bot import handle_message, get_active_reg
from editor import handle_edit_message, is_edit_request, get_editing_reg


class Registrar:
    in_edit          = staticmethod(lambda phone: get_editing_reg(phone) is not None)
    in_registration  = staticmethod(lambda phone: get_active_reg(phone) is not None)
    wants_edit       = staticmethod(is_edit_request)
    edit             = staticmethod(handle_edit_message)    # يعدّل + يحدّث DB
    handle           = staticmethod(handle_message)         # يجمع البيانات محادثةً
