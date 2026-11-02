#!/usr/bin/env python3
"""Automatizeaza integrarea angajatilor noi pentru proiectul BPA B."""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


USER_AGENT = "bpa-onboarding-solution/1.0"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_RO_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
CNP_WEIGHTS = (2, 7, 9, 1, 4, 6, 3, 5, 8, 2, 7, 9)
TRANSLITERATION = str.maketrans(
    {
        "ă": "a", "â": "a", "î": "i", "ș": "s", "ş": "s", "ț": "t", "ţ": "t",
        "Ă": "a", "Â": "a", "Î": "i", "Ș": "s", "Ş": "s", "Ț": "t", "Ţ": "t",
    }
)


class HubError(Exception):
    def __init__(self, method: str, path: str, status: int | None, code: str, message: str) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.code = code
        super().__init__(f"{method} {path}: {status or 'network'} {code}: {message}")


class Hub:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def _retry_after(headers: Any) -> float:
        try:
            return max(0.0, float(headers.get("Retry-After", "1")))
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def _error_body(raw: bytes) -> tuple[str, str]:
        try:
            error = json.loads(raw or b"{}").get("error", {})
            return str(error.get("code", "HTTP_ERROR")), str(error.get("message", ""))
        except (ValueError, AttributeError):
            return "HTTP_ERROR", raw.decode("utf-8", errors="replace")[:500]

    def call(self, method: str, path: str, body: Any = None, idempotency_key: str | None = None) -> Any:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        attempts = 0
        rate_limits = 0
        while attempts < 3:
            request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    raw = response.read()
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as error:
                code, message = self._error_body(error.read())
                if error.code == 429:
                    if code == "RUN_REQUEST_LIMIT":
                        raise HubError(method, path, error.code, code, message) from None
                    rate_limits += 1
                    if rate_limits > 10:
                        raise HubError(method, path, error.code, code, message) from None
                    delay = self._retry_after(error.headers)
                    print(f"Retry {method} {path}: 429 {code}, waiting {delay:g}s", file=sys.stderr, flush=True)
                    time.sleep(delay)
                    continue
                if error.code not in (500, 502, 503, 504):
                    raise HubError(method, path, error.code, code, message) from None
                attempts += 1
                if attempts == 3:
                    raise HubError(method, path, error.code, code, message) from None
                delay = self._retry_after(error.headers)
                print(
                    f"Retry {method} {path}: HTTP {error.code}, attempt {attempts + 1}/3, waiting {delay:g}s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, socket.timeout) as error:
                attempts += 1
                if attempts == 3:
                    raise HubError(method, path, None, "NETWORK_ERROR", str(error)) from None
                print(
                    f"Retry {method} {path}: network error, attempt {attempts + 1}/3, waiting 1s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(1)
        raise HubError(method, path, None, "NETWORK_ERROR", "retry limit reached")

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if params:
            path += "?" + urllib.parse.urlencode(params)
        return self.call("GET", path)

    def post(self, path: str, body: dict[str, Any], key: str) -> Any:
        return self.call("POST", path, body, key)


def log_result(request_id: str, outcome: str, reason_codes: list[str]) -> None:
    print(f"{request_id} | {outcome} | {','.join(reason_codes)}", file=sys.stderr, flush=True)


def index_records(records: list[dict[str, Any]], *keys: str) -> dict[str, dict[str, Any]]:
    result = {}
    for record in records:
        for key in keys:
            value = record.get(key)
            if isinstance(value, str) and value:
                result[value.strip().upper()] = record
                break
    return result


def parse_start_date(value: str) -> date | None:
    if DATE_ISO_RE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    if DATE_RO_RE.fullmatch(value):
        try:
            return datetime.strptime(value, "%d.%m.%Y").date()
        except ValueError:
            return None
    return None


def normalize_name(value: str) -> str:
    value = value.strip().replace("ş", "ș").replace("Ş", "Ș").replace("ţ", "ț").replace("Ţ", "Ț")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s*-\s*", "-", value)
    return " ".join("-".join(part.capitalize() for part in word.split("-")) for word in value.split(" "))


def normalize_document(document: Any) -> tuple[dict[str, Any], bool]:
    required = (
        "request_type", "first_name", "last_name", "cnp", "personal_email",
        "start_date", "role_code", "department_code",
    )
    if not isinstance(document, dict):
        return {}, False
    if any(not isinstance(document.get(field), str) or not document[field].strip() for field in required):
        return {}, False

    normalized = dict(document)
    for field in required:
        normalized[field] = document[field].strip()
    if normalized["request_type"] != "NEW_HIRE":
        return normalized, False

    start_date = parse_start_date(normalized["start_date"])
    email = normalized["personal_email"].lower()
    if start_date is None or not EMAIL_RE.fullmatch(email):
        return normalized, False

    normalized["first_name"] = normalize_name(normalized["first_name"])
    normalized["last_name"] = normalize_name(normalized["last_name"])
    normalized["personal_email"] = email
    normalized["start_date"] = start_date.isoformat()
    normalized["role_code"] = normalized["role_code"].upper()
    normalized["department_code"] = normalized["department_code"].upper()
    manager_id = document.get("manager_id")
    normalized["manager_id"] = manager_id.strip().upper() if isinstance(manager_id, str) else manager_id
    return normalized, True


def valid_cnp(value: str) -> bool:
    if not re.fullmatch(r"\d{13}", value):
        return False
    series = int(value[0])
    county = int(value[7:9])
    serial = value[9:12]
    if series not in range(1, 7) or county not in (*range(1, 47), 51, 52) or serial == "000":
        return False
    birth_year = int(value[1:3])
    century = 1900 if series in (1, 2) else 1800 if series in (3, 4) else 2000
    try:
        date(century + birth_year, int(value[3:5]), int(value[5:7]))
    except ValueError:
        return False
    checksum = sum(int(digit) * weight for digit, weight in zip(value[:12], CNP_WEIGHTS)) % 11
    expected = 1 if checksum == 10 else checksum
    return int(value[12]) == expected


def age_at_start(cnp: str, start_date: date) -> int:
    series = int(cnp[0])
    century = 1900 if series in (1, 2) else 1800 if series in (3, 4) else 2000
    birthday = date(century + int(cnp[1:3]), int(cnp[3:5]), int(cnp[5:7]))
    try:
        anniversary = birthday.replace(year=start_date.year)
    except ValueError:
        anniversary = date(start_date.year, 3, 1)
    return start_date.year - birthday.year - (start_date < anniversary)


def add_reason(codes: list[str], code: str) -> None:
    if code not in codes:
        codes.append(code)


def date_value(record: dict[str, Any]) -> date | None:
    value = record.get("date") or record.get("holiday_date")
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def is_workday(day: date, holidays: set[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def add_workdays(start: date, count: int, holidays: set[date]) -> date:
    current = start
    added = 0
    while added < count:
        current += timedelta(days=1)
        if is_workday(current, holidays):
            added += 1
    return current


def username_base(first_name: str, last_name: str) -> str:
    first = re.split(r"[ -]", first_name, maxsplit=1)[0].translate(TRANSLITERATION).lower()
    last = re.sub(r"\s+", "-", last_name).translate(TRANSLITERATION).lower()
    return re.sub(r"[^a-z-]", "", first) + "." + re.sub(r"[^a-z-]", "", last)


def unique_username(base: str, accounts: list[dict[str, Any]]) -> str:
    used = {str(account.get("username", "")).casefold() for account in accounts}
    candidate = base
    suffix = 2
    while candidate.casefold() in used:
        candidate = f"{base}{suffix}"
        suffix += 1
    return candidate


def record_code(record: dict[str, Any], *fields: str) -> str:
    for field in fields:
        value = record.get(field)
        if isinstance(value, str):
            return value
    return ""


class Onboarding:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.context = hub.get("/context")
        self.config = hub.get("/config")
        self.roles = index_records(hub.get("/roles"), "role_code", "code")
        self.departments = index_records(hub.get("/departments"), "department_code", "code")
        self.packages = index_records(hub.get("/access-packages"), "package_code", "code")
        self.trainings = index_records(hub.get("/trainings"), "training_code", "code")
        employees = hub.get("/employees")
        self.employees = index_records(employees, "employee_id", "id", "manager_id")
        self.holidays = {day for item in hub.get("/holidays") if (day := date_value(item)) is not None}
        self.accounts = hub.get("/directory/accounts")
        self.business_date = date.fromisoformat(self.context["business_date"])
        self.results: dict[str, dict[str, Any]] = {}

    def manager_for(self, document: dict[str, Any]) -> dict[str, Any] | None:
        manager_id = document.get("manager_id")
        if not isinstance(manager_id, str) or not manager_id.strip():
            return None
        return self.employees.get(manager_id.strip().upper())

    def validation_codes(self, document: dict[str, Any], valid: bool, request_id: str) -> tuple[list[str], dict[str, Any] | None]:
        if not valid:
            return ["INVALID_REQUEST"], None

        codes: list[str] = []
        start_date = date.fromisoformat(document["start_date"])
        cnp_ok = valid_cnp(document["cnp"])
        if not cnp_ok:
            add_reason(codes, "INVALID_CNP")
        else:
            age = age_at_start(document["cnp"], start_date)
            minimum_age = int(self.config.get("minimum_age", 16))
            adult_age = int(self.config.get("adult_age", 18))
            if age < minimum_age:
                add_reason(codes, "UNDERAGE")
            elif age < adult_age:
                add_reason(codes, "MINOR_EMPLOYEE")

        role = self.roles.get(document["role_code"])
        department = self.departments.get(document["department_code"])
        if role is None:
            add_reason(codes, "UNKNOWN_ROLE")
        if department is None:
            add_reason(codes, "UNKNOWN_DEPARTMENT")

        if start_date < self.business_date:
            add_reason(codes, "START_DATE_IN_PAST")

        has_unknown_reference = role is None or department is None
        if not has_unknown_reference:
            allowed = {str(value).strip().upper() for value in role.get("allowed_departments", [])}
            if document["department_code"] not in allowed:
                add_reason(codes, "ROLE_NOT_IN_DEPARTMENT")

        manager_id = document.get("manager_id")
        manager = self.manager_for(document)
        if not isinstance(manager_id, str) or not manager_id.strip() or manager is None:
            add_reason(codes, "MANAGER_MISSING")
        elif str(manager.get("status", "")).upper() != "ACTIVE":
            add_reason(codes, "MANAGER_INACTIVE")

        if cnp_ok:
            duplicate = any(
                account.get("cnp") == document["cnp"] and account.get("request_id") != request_id
                for account in self.accounts
            )
            if duplicate:
                add_reason(codes, "ALREADY_EMPLOYED")

        if not has_unknown_reference and "ROLE_NOT_IN_DEPARTMENT" not in codes:
            selected = set(role.get("base_packages", [])) | set(department.get("addon_packages", []))
            conflict = any(
                other in selected
                for package_code in selected
                for other in self.packages.get(package_code, {}).get("conflicts_with", [])
                if other != package_code
            )
            if conflict:
                add_reason(codes, "SOD_CONFLICT")

        return codes, manager

    @staticmethod
    def outcome_for_validation(codes: list[str]) -> str | None:
        rejected = {
            "INVALID_REQUEST", "INVALID_CNP", "UNDERAGE", "UNKNOWN_ROLE",
            "UNKNOWN_DEPARTMENT", "START_DATE_IN_PAST",
        }
        manual = {
            "MINOR_EMPLOYEE", "ROLE_NOT_IN_DEPARTMENT", "MANAGER_MISSING",
            "MANAGER_INACTIVE", "ALREADY_EMPLOYED", "SOD_CONFLICT",
        }
        if any(code in rejected for code in codes):
            return "REJECTED"
        if any(code in manual for code in codes):
            return "MANUAL_REVIEW"
        return None

    def write_record(
        self, request_id: str, outcome: str, codes: list[str], steps: dict[str, str]
    ) -> dict[str, Any] | None:
        try:
            self.hub.post(
                "/onboarding-records",
                {"request_id": request_id, "outcome": outcome, "reason_codes": codes, "steps": steps},
                f"{request_id}:record",
            )
        except HubError as error:
            if error.code != "IDEMPOTENCY_KEY_REUSED":
                raise
            history = self.hub.get("/history", {"request_id": request_id})
            latest = history[-1] if history else None
            if latest and latest.get("source") == "THIS_RUN":
                return latest
            raise
        return None

    def account_step(self, request_id: str, document: dict[str, Any]) -> tuple[str, str | None]:
        existing = next(
            (
                account for account in self.accounts
                if account.get("cnp") == document["cnp"] and account.get("request_id") == request_id
            ),
            None,
        )
        if existing:
            return "DONE", str(existing.get("username", ""))

        username = unique_username(username_base(document["first_name"], document["last_name"]), self.accounts)
        body = {
            "request_id": request_id,
            "username": username,
            "first_name": document["first_name"],
            "last_name": document["last_name"],
            "cnp": document["cnp"],
            "role_code": document["role_code"],
            "department_code": document["department_code"],
            "start_date": document["start_date"],
        }
        try:
            self.hub.post("/directory/accounts", body, f"{request_id}:account")
        except HubError as error:
            if error.status == 500:
                return "FAILED", None
            if error.code == "IDEMPOTENCY_KEY_REUSED":
                self.accounts = self.hub.get("/directory/accounts")
                existing = next(
                    (
                        account for account in self.accounts
                        if account.get("cnp") == document["cnp"] and account.get("request_id") == request_id
                    ),
                    None,
                )
                if existing:
                    return "DONE", str(existing.get("username", ""))
            raise
        self.accounts.append(body)
        return "DONE", username

    def write_repeated(
        self, path: str, body: dict[str, Any], key: str, confirmed: Any = None
    ) -> bool:
        try:
            self.hub.post(path, body, key)
            return True
        except HubError as error:
            if error.code == "IDEMPOTENCY_KEY_REUSED" and confirmed is not None and confirmed():
                return True
            if error.status == 500:
                return False
            raise

    def access_step(self, request_id: str, document: dict[str, Any], username: str) -> str:
        role = self.roles[document["role_code"]]
        department = self.departments[document["department_code"]]
        package_codes = sorted(set(role.get("base_packages", [])) | set(department.get("addon_packages", [])))
        existing = self.hub.get("/access/grants", {"request_id": request_id})
        granted = {item.get("package_code") for item in existing}
        for package_code in package_codes:
            if package_code in granted:
                continue
            body = {"request_id": request_id, "username": username, "package_code": package_code}
            if not self.write_repeated(
                "/access/grants",
                body,
                f"{request_id}:grant:{package_code}",
                lambda: any(
                    item.get("package_code") == package_code
                    for item in self.hub.get("/access/grants", {"request_id": request_id})
                ),
            ):
                return "FAILED"
        return "DONE"

    def training_step(self, request_id: str, document: dict[str, Any], username: str) -> str:
        role = self.roles[document["role_code"]]
        required = {
            code for code, training in self.trainings.items() if training.get("mandatory") is True
        } | set(role.get("required_trainings", []))
        existing = self.hub.get("/lms/enrollments", {"request_id": request_id})
        enrolled = {item.get("training_code") for item in existing}
        start_date = date.fromisoformat(document["start_date"])
        for training_code in sorted(required):
            if training_code in enrolled:
                continue
            training = self.trainings.get(training_code, {})
            due_date = add_workdays(start_date, int(training.get("days_to_complete", 0)), self.holidays)
            body = {
                "request_id": request_id,
                "username": username,
                "training_code": training_code,
                "due_date": due_date.isoformat(),
            }
            if not self.write_repeated(
                "/lms/enrollments",
                body,
                f"{request_id}:training:{training_code}",
                lambda: any(
                    item.get("training_code") == training_code
                    for item in self.hub.get("/lms/enrollments", {"request_id": request_id})
                ),
            ):
                return "FAILED"
        return "DONE"

    def meeting_days(self, start_date: date) -> list[date]:
        count = int(self.config.get("meeting", {}).get("window_working_days", 3))
        days = []
        current = start_date
        while len(days) < count:
            if is_workday(current, self.holidays):
                days.append(current)
            current += timedelta(days=1)
        return days

    def meeting_step(
        self, request_id: str, document: dict[str, Any], username: str, manager: dict[str, Any]
    ) -> str:
        existing = self.hub.get("/calendar/meetings", {"request_id": request_id})
        if existing:
            return "DONE"

        days = self.meeting_days(date.fromisoformat(document["start_date"]))
        if not days:
            return "NO_SLOT"
        employee_id = manager.get("employee_id", manager.get("id", document["manager_id"]))
        busy = []
        first_day = days[0]
        while first_day <= days[-1]:
            last_day = min(first_day + timedelta(days=62), days[-1])
            busy.extend(
                self.hub.get(
                    "/calendar/busy",
                    {"employee_id": employee_id, "from": first_day.isoformat(), "to": last_day.isoformat()},
                )
            )
            first_day = last_day + timedelta(days=1)
        meeting = self.config.get("meeting", {})
        duration = int(meeting.get("duration_minutes", 60))
        step = int(meeting.get("slot_minutes", 30))
        day_start = datetime.strptime(meeting.get("day_start", "09:00"), "%H:%M").time()
        day_end = datetime.strptime(meeting.get("day_end", "17:00"), "%H:%M").time()
        timezone = ZoneInfo(meeting.get("timezone", "Europe/Bucharest"))
        intervals = [
            (datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"]))
            for item in busy
        ]

        for day in days:
            start = datetime.combine(day, day_start, timezone)
            end_of_day = datetime.combine(day, day_end, timezone)
            while start + timedelta(minutes=duration) <= end_of_day:
                end = start + timedelta(minutes=duration)
                if not any(start < busy_end and busy_start < end for busy_start, busy_end in intervals):
                    body = {
                        "request_id": request_id,
                        "manager_id": document["manager_id"],
                        "username": username,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                    }
                    if self.write_repeated(
                        "/calendar/meetings",
                        body,
                        f"{request_id}:meeting",
                        lambda: bool(self.hub.get("/calendar/meetings", {"request_id": request_id})),
                    ):
                        return "DONE"
                    return "FAILED"
                start += timedelta(minutes=step)
        return "NO_SLOT"

    def notification_specs(self, outcome: str, steps: dict[str, str]) -> list[tuple[str, str]]:
        specs = {
            "COMPLETED": [("MANAGER", "onboarding_completed"), ("IT", "it_setup"), ("EMPLOYEE", "welcome")],
            "PARTIAL": [("HR", "onboarding_partial"), ("IT_OPS", "onboarding_partial")],
            "MANUAL_REVIEW": [("HR", "manual_review")],
            "REJECTED": [("HR", "request_rejected")],
        }[outcome]
        if outcome in ("COMPLETED", "PARTIAL") and steps.get("meeting") == "NO_SLOT":
            specs.append(("HR", "meeting_not_scheduled"))
        return specs

    def send_notifications(
        self,
        request_id: str,
        document: dict[str, Any],
        manager: dict[str, Any] | None,
        outcome: str,
        steps: dict[str, str],
        existing: list[dict[str, Any]] | None = None,
    ) -> None:
        existing = existing if existing is not None else self.hub.get("/notifications", {"request_id": request_id})
        sent = {(item.get("recipient_role"), item.get("template")) for item in existing}
        recipients = self.config.get("notification_recipients", {})
        for recipient_role, template in self.notification_specs(outcome, steps):
            if (recipient_role, template) in sent:
                continue
            if recipient_role == "MANAGER":
                recipient = manager.get("email") if manager else None
            elif recipient_role == "EMPLOYEE":
                recipient = document.get("personal_email")
            else:
                recipient = recipients.get(recipient_role)
            if not isinstance(recipient, str) or not recipient:
                raise HubError("POST", "/notifications", None, "MISSING_RECIPIENT", recipient_role)
            try:
                self.hub.post(
                    "/notifications",
                    {"request_id": request_id, "recipient_role": recipient_role, "template": template, "recipient": recipient},
                    f"{request_id}:notify:{recipient_role}:{template}",
                )
            except HubError as error:
                if error.code != "IDEMPOTENCY_KEY_REUSED":
                    raise
                latest = self.hub.get("/notifications", {"request_id": request_id})
                if not any(
                    item.get("recipient_role") == recipient_role and item.get("template") == template
                    for item in latest
                ):
                    raise
            sent.add((recipient_role, template))

    def finish_record(
        self,
        request_id: str,
        document: dict[str, Any],
        manager: dict[str, Any] | None,
        outcome: str,
        codes: list[str],
        steps: dict[str, str],
    ) -> None:
        existing = self.write_record(request_id, outcome, codes, steps)
        if existing:
            outcome = existing["outcome"]
            codes = list(existing.get("reason_codes", []))
            steps = existing.get("steps", steps)
        self.send_notifications(request_id, document, manager, outcome, steps)
        self.results[request_id] = {"outcome": outcome, "reason_codes": codes}
        log_result(request_id, outcome, codes)

    def process(self, request_id: str) -> None:
        history = self.hub.get("/history", {"request_id": request_id})
        latest = history[-1] if history else None
        if latest and latest.get("source") == "THIS_RUN":
            document, _ = normalize_document(self.hub.get(f"/requests/{urllib.parse.quote(request_id, safe='')}"))
            notifications = self.hub.get("/notifications", {"request_id": request_id})
            outcome = latest["outcome"]
            codes = list(latest.get("reason_codes", []))
            steps = latest.get("steps", {})
            self.send_notifications(request_id, document, self.manager_for(document), outcome, steps, notifications)
            self.results[request_id] = {"outcome": outcome, "reason_codes": codes}
            log_result(request_id, outcome, codes)
            return

        if latest and latest.get("source") == "EARLIER_RUN" and latest.get("outcome") in {
            "COMPLETED", "REJECTED", "MANUAL_REVIEW",
        }:
            self.results[request_id] = {
                "outcome": latest["outcome"],
                "reason_codes": list(latest.get("reason_codes", [])),
            }
            log_result(request_id, latest["outcome"], self.results[request_id]["reason_codes"])
            return

        raw_document = self.hub.get(f"/requests/{urllib.parse.quote(request_id, safe='')}")
        document, valid = normalize_document(raw_document)
        codes, manager = self.validation_codes(document, valid, request_id)
        outcome = self.outcome_for_validation(codes)
        if outcome:
            steps = {"account": "SKIPPED", "access": "SKIPPED", "training": "SKIPPED", "meeting": "SKIPPED"}
            self.finish_record(request_id, document, manager, outcome, codes, steps)
            return

        steps = {"account": "SKIPPED", "access": "SKIPPED", "training": "SKIPPED", "meeting": "SKIPPED"}
        steps["account"], username = self.account_step(request_id, document)
        if steps["account"] == "FAILED":
            add_reason(codes, "ACCOUNT_FAILED")
        else:
            assert username is not None
            steps["access"] = self.access_step(request_id, document, username)
            if steps["access"] == "FAILED":
                add_reason(codes, "ACCESS_FAILED")

            steps["training"] = self.training_step(request_id, document, username)
            if steps["training"] == "FAILED":
                add_reason(codes, "TRAINING_FAILED")

            steps["meeting"] = self.meeting_step(request_id, document, username, manager)
            if steps["meeting"] == "FAILED":
                add_reason(codes, "MEETING_FAILED")
            elif steps["meeting"] == "NO_SLOT":
                add_reason(codes, "MEETING_NOT_SCHEDULED")

        outcome = "PARTIAL" if "FAILED" in steps.values() else "COMPLETED"
        self.finish_record(request_id, document, manager, outcome, codes, steps)

    def report(self, request_ids: list[str]) -> None:
        items = [
            {
                "request_id": request_id,
                "outcome": self.results[request_id]["outcome"],
                "reason_codes": self.results[request_id]["reason_codes"],
            }
            for request_id in request_ids
        ]
        body = {"items": items}
        suffix = 1
        while True:
            key = "report" if suffix == 1 else f"report-{suffix}"
            try:
                self.hub.post("/report", body, key)
                return
            except HubError as error:
                if error.status != 422 or error.code != "IDEMPOTENCY_KEY_REUSED":
                    raise
                suffix += 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    base_url = argv[0] if argv else os.environ.get("BPA_BASE_URL")
    if not base_url:
        print("Folosire: python solutie.py BASE_URL (sau seteaza BPA_BASE_URL)", file=sys.stderr)
        return 2

    try:
        solution = Onboarding(Hub(base_url))
        requests = solution.hub.get("/requests")
        request_ids = [item["request_id"] for item in requests]
        for request_id in request_ids:
            solution.process(request_id)
        solution.report(request_ids)
        return 0
    except (HubError, KeyError, TypeError, ValueError) as error:
        print(f"Eroare: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())