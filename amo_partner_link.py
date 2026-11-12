#!/usr/bin/env python3
"""
Прикрепление партнёра к партнёрским лидам в amoCRM.

Заменяет связку Albato «Поиск лидов партнера» (479713).

Что делает для каждого нового лида в воронке «Партнерство: лидогенерация»:
  1. Определяет код партнёра: utm_medium / last utm_medium / referrer лида.
     Лид считается партнёрским, если utm_source или last_utm_source = partner,
     либо если в поле referrer стоит числовой код (лиды из виджета партнёра).
  2. Находит сделку партнёра в воронке «Партнерство» по коду. Код сверяется
     с ID сделки, с полем «referrer партнера» и с полем «utm medium партнера».
     Так находятся и партнёры, у которых код не совпадает с ID сделки
     (например, PRMOnline: код 31358193, сделка 33583531).
  3. Пишет компанию партнёра в поле лида «Партнер» (707209),
     привязывает к лиду основной контакт партнёра,
     оставляет примечание со ссылкой на сделку партнёра,
     ставит тег, чтобы не обрабатывать лид повторно.
  4. Если кода нет или партнёр не найден — ставит задачу ответственному
     (как связка Albato) и тоже помечает лид тегом.

Запуск:
  python amo_partner_link.py              # dry-run: только показать, что будет сделано
  python amo_partner_link.py --apply      # записать изменения в amoCRM
  python amo_partner_link.py --days 7     # окно по дате создания лида (по умолчанию 3 дня)
  python amo_partner_link.py --lead 34360627 [--apply]   # один конкретный лид

Переменные окружения:
  AMO_SUBDOMAIN (по умолчанию solarstaff)
  AMO_TOKEN     долгосрочный токен amoCRM
"""

import argparse
import json
import os
import re
import sys
import time

import requests

# ---------------------------------------------------------------- настройки
SUBDOMAIN = os.environ.get("AMO_SUBDOMAIN", "solarstaff")
TOKEN = os.environ.get("AMO_TOKEN", "")
BASE = f"https://{SUBDOMAIN}.amocrm.ru"

LEADS_PIPELINE_ID = int(os.environ.get("LEADS_PIPELINE_ID", 11489426))   # Партнерство: лидогенерация
PARTNERS_PIPELINE_ID = int(os.environ.get("PARTNERS_PIPELINE_ID", 8923730))  # Партнерство

# поля лида
F_UTM_SOURCE = 645259
F_LAST_UTM_SOURCE = 705866
F_UTM_MEDIUM = 645261
F_LAST_UTM_MEDIUM = 705868
F_REFERRER = 645275
F_PARTNER_LINK = 707209          # «Партнер» — связь с компанией партнёра

# поля сделки партнёра
F_PARTNER_PERSONAL_URL = 707793  # Персональная ссылка партнера
F_PARTNER_REFERRER = 705296      # referrer партнера
F_PARTNER_UTM_MEDIUM = 705258    # utm medium партнера

# задача, если партнёра определить не удалось (как в Albato)
TASK_TYPE_ID = int(os.environ.get("TASK_TYPE_ID", 3537886))
TASK_RESPONSIBLE_ID = int(os.environ.get("TASK_RESPONSIBLE_ID", 11385786))

TAG_DONE = "partner_linked"
TAG_FAILED = "partner_not_found"

PARTNER_UTM = "partner"


# ---------------------------------------------------------------- HTTP
session = requests.Session()
session.headers.update({"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})


def api(method, path, **kw):
    url = BASE + path
    for attempt in range(6):
        try:
            r = session.request(method, url, timeout=60, **kw)
        except requests.exceptions.RequestException as e:
            wait = min(60, 3 * 2 ** attempt)
            print(f"  ! сеть: {e.__class__.__name__}, повтор через {wait} c", file=sys.stderr)
            time.sleep(wait)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            wait = min(60, 3 * 2 ** attempt)
            print(f"  ! {r.status_code} от amo, повтор через {wait} c", file=sys.stderr)
            time.sleep(wait)
            continue
        if r.status_code == 204:
            return None
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        time.sleep(0.15)  # amo: не больше 7 запросов в секунду
        return r.json()
    raise RuntimeError(f"{method} {path}: не удалось после повторов")


def paged(path, params, key):
    page = 1
    while True:
        p = dict(params, page=page, limit=250)
        data = api("GET", path, params=p)
        if not data:
            return
        items = data.get("_embedded", {}).get(key, [])
        yield from items
        if not data.get("_links", {}).get("next"):
            return
        page += 1


# ---------------------------------------------------------------- утилиты
def cf(entity, field_id):
    for f in entity.get("custom_fields_values") or []:
        if f.get("field_id") == field_id and f.get("values"):
            return f["values"][0].get("value")
    return None


def norm(v):
    return str(v).strip().lower() if v is not None else ""


def code_of(v):
    """Числовой код партнёра из значения поля (берём первое число из 6+ цифр)."""
    if v is None:
        return None
    m = re.search(r"\d{6,}", str(v))
    return m.group(0) if m else None


def tags_of(lead):
    return {t["name"] for t in lead.get("_embedded", {}).get("tags", [])}


# ---------------------------------------------------------------- партнёры
def load_partners():
    """Индекс: код -> сделка партнёра (из воронки «Партнерство»)."""
    index = {}
    for deal in paged("/api/v4/leads", {"filter[pipeline_id][]": PARTNERS_PIPELINE_ID}, "leads"):
        codes = {str(deal["id"])}
        for fid in (F_PARTNER_REFERRER, F_PARTNER_UTM_MEDIUM):
            c = code_of(cf(deal, fid))
            if c:
                codes.add(c)
        for c in codes:
            # ID сделки имеет приоритет над кодами из полей
            if c not in index or c == str(deal["id"]):
                index[c] = deal
    return index


def find_partner(code, index):
    if code in index:
        return index[code]
    # запасной путь, как в Albato: сделка с таким ID в любой воронке
    try:
        deal = api("GET", f"/api/v4/leads/{code}")
    except RuntimeError:
        deal = None
    return deal


def partner_contact_and_company(deal_id):
    deal = api("GET", f"/api/v4/leads/{deal_id}", params={"with": "contacts"})
    emb = (deal or {}).get("_embedded", {})
    contacts = emb.get("contacts", [])
    main = next((c for c in contacts if c.get("is_main")), contacts[0] if contacts else None)
    companies = emb.get("companies", [])
    return (main["id"] if main else None), (companies[0]["id"] if companies else None)


def partner_tag_name(contact_id, company_id):
    """Тег лида: название компании партнёра, а если его нет — полное имя контакта."""
    if company_id:
        company = api("GET", f"/api/v4/companies/{company_id}") or {}
        name = (company.get("name") or "").strip()
        if name:
            return name
    if contact_id:
        contact = api("GET", f"/api/v4/contacts/{contact_id}") or {}
        name = (contact.get("name") or "").strip()
        if not name:
            name = " ".join(x for x in (contact.get("first_name"), contact.get("last_name")) if x).strip()
        if name:
            return name
    return None


# ---------------------------------------------------------------- лиды
def is_partner_lead(lead):
    if PARTNER_UTM in (norm(cf(lead, F_UTM_SOURCE)), norm(cf(lead, F_LAST_UTM_SOURCE))):
        return True
    return code_of(cf(lead, F_REFERRER)) is not None


def lead_code(lead):
    for fid in (F_UTM_MEDIUM, F_LAST_UTM_MEDIUM, F_REFERRER):
        c = code_of(cf(lead, fid))
        if c:
            return c, fid
    return None, None


def load_leads(days, lead_id=None):
    if lead_id:
        return [api("GET", f"/api/v4/leads/{lead_id}", params={"with": "contacts"})]
    since = int(time.time()) - days * 86400
    params = {
        "filter[pipeline_id][]": LEADS_PIPELINE_ID,
        "filter[created_at][from]": since,
        "with": "contacts",
    }
    return list(paged("/api/v4/leads", params, "leads"))


# ---------------------------------------------------------------- действия
def add_tag(lead_id, tag):
    api("PATCH", f"/api/v4/leads/{lead_id}", json={"tags_to_add": [{"name": tag}]})


def create_task(lead, text):
    api("POST", "/api/v4/tasks", json=[{
        "task_type_id": TASK_TYPE_ID,
        "text": text,
        "complete_till": int(time.time()) + 60,
        "entity_id": lead["id"],
        "entity_type": "leads",
        "responsible_user_id": TASK_RESPONSIBLE_ID,
    }])


def partner_referrer(partner, code):
    """Код партнёра для поля лида «referrer партнера»: из карточки партнёра, иначе найденный код."""
    return code_of(cf(partner, F_PARTNER_REFERRER)) or code


def fill_referrer_and_tag(lead, ref, tag_name):
    """Дозаполнить «referrer партнера» и тег с названием партнёра у уже обработанного лида."""
    body = {"custom_fields_values": [{"field_id": F_PARTNER_REFERRER, "values": [{"value": str(ref)}]}]}
    if tag_name and tag_name not in tags_of(lead):
        body["tags_to_add"] = [{"name": tag_name}]
    api("PATCH", f"/api/v4/leads/{lead['id']}", json=body)


def attach_partner(lead, partner, contact_id, company_id, tag_name, ref):
    lead_id = lead["id"]
    fields = [{"field_id": F_PARTNER_REFERRER, "values": [{"value": str(ref)}]}]
    if company_id:
        value = json.dumps({"contacts": [], "leads": [], "companies": [company_id], "customers": []})
        fields.append({"field_id": F_PARTNER_LINK, "values": [{"value": value}]})
    api("PATCH", f"/api/v4/leads/{lead_id}", json={"custom_fields_values": fields})
    linked = {c["id"] for c in lead.get("_embedded", {}).get("contacts", [])}
    if contact_id and contact_id not in linked:
        api("POST", f"/api/v4/leads/{lead_id}/link", json=[{"to_entity_id": contact_id, "to_entity_type": "contacts"}])
    url = cf(partner, F_PARTNER_PERSONAL_URL) or "—"
    text = (f"⛳️ Лид привлечен партнером: {BASE}/leads/detail/{partner['id']} "
            f"с помощью персональной ссылки: {url}\nПартнер прикреплен к текущей сделке!")
    api("POST", f"/api/v4/leads/{lead_id}/notes", json=[{"note_type": "common", "params": {"text": text}}])
    tags = [{"name": TAG_DONE}]
    if tag_name:
        tags.append({"name": tag_name})
    api("PATCH", f"/api/v4/leads/{lead_id}", json={"tags_to_add": tags})


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="записать изменения (без флага — dry-run)")
    ap.add_argument("--days", type=int, default=3, help="окно по дате создания лида, дней")
    ap.add_argument("--lead", type=int, help="обработать один лид по ID")
    args = ap.parse_args()

    if not TOKEN:
        sys.exit("Нет AMO_TOKEN")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] воронка {LEADS_PIPELINE_ID}, окно {args.days} дн." + (f", лид {args.lead}" if args.lead else ""))

    partners = load_partners()
    print(f"Партнёров в индексе: {len({d['id'] for d in partners.values()})}, кодов: {len(partners)}")

    leads = load_leads(args.days, args.lead)
    stats = {"linked": 0, "repaired": 0, "no_code": 0, "not_found": 0, "skipped": 0}

    for lead in leads:
        lid = lead["id"]
        tags = tags_of(lead)
        if not args.lead and TAG_FAILED in tags:
            stats["skipped"] += 1
            continue
        if lead.get("pipeline_id") != LEADS_PIPELINE_ID and not args.lead:
            stats["skipped"] += 1
            continue
        if not is_partner_lead(lead):
            stats["skipped"] += 1
            continue

        already = TAG_DONE in tags or bool(cf(lead, F_PARTNER_LINK))
        if not args.lead and already:
            # партнёр уже прикреплён (скриптом или ещё связкой Albato):
            # только дозаполняем «referrer партнера» и тег, если их нет
            if cf(lead, F_PARTNER_REFERRER):
                stats["skipped"] += 1
                continue
            code, src = lead_code(lead)
            partner = find_partner(code, partners) if code else None
            if not partner:
                stats["skipped"] += 1
                continue
            ref = partner_referrer(partner, code)
            contact_id, company_id = partner_contact_and_company(partner["id"])
            tag_name = partner_tag_name(contact_id, company_id)
            print(f"  {lid}: уже прикреплён -> дозаполняю referrer партнера {ref}, тег «{tag_name}»  "
                  f"{BASE}/leads/detail/{lid}")
            stats["repaired"] += 1
            if args.apply:
                fill_referrer_and_tag(lead, ref, tag_name)
            continue

        code, src = lead_code(lead)
        if not code:
            stats["no_code"] += 1
            print(f"  {lid}: нет кода партнёра -> задача")
            if args.apply:
                create_task(lead, "Нет utm_source партнера, неясно, кто привлек лид ⚙️")
                add_tag(lid, TAG_FAILED)
            continue

        partner = find_partner(code, partners)
        if not partner:
            stats["not_found"] += 1
            print(f"  {lid}: код {code} (поле {src}) — партнёр не найден -> задача")
            if args.apply:
                create_task(lead, f"Не найден партнёр по коду {code} ⚙️ Проверьте, кто привлек лид.")
                add_tag(lid, TAG_FAILED)
            continue

        contact_id, company_id = partner_contact_and_company(partner["id"])
        tag_name = partner_tag_name(contact_id, company_id)
        ref = partner_referrer(partner, code)
        print(f"  {lid}: код {code} (поле {src}) -> партнёр «{partner.get('name')}» {partner['id']}, "
              f"контакт {contact_id}, компания {company_id}, referrer {ref}, тег «{tag_name}»  "
              f"{BASE}/leads/detail/{lid}")
        stats["linked"] += 1
        if args.apply:
            attach_partner(lead, partner, contact_id, company_id, tag_name, ref)

    print(f"Итого: прикреплено {stats['linked']}, дозаполнено {stats['repaired']}, без кода {stats['no_code']}, "
          f"партнёр не найден {stats['not_found']}, пропущено {stats['skipped']}")


if __name__ == "__main__":
    main()
