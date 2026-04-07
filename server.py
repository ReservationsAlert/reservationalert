#!/usr/bin/env python3
"""
ReservationAlert.ai - MVP Backend Server
Zero-dependency Python server (uses only standard library)
"""

import http.server
import hashlib
import hmac
import json
import secrets
import sqlite3
import os
import uuid
import threading
import time
import urllib.request
import urllib.error
import re
from datetime import datetime, timedelta
from pathlib import Path
from functools import partial

# ── Configuration ────────────────────────────────────────────────────────────

PORT = int(os.environ.get("PORT", 8080))
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "reservationalert.db"))
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL", 300))  # 5 minutes default

# Email config via Resend (set RESEND_API_KEY env var)
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "ReservationAlert <noreply@reservationalert.ai>")
BASE_URL = os.environ.get("BASE_URL", "https://reservationalert.onrender.com")

# Auth config
SESSION_EXPIRY_DAYS = 30
MAGIC_LINK_EXPIRY_MINUTES = 15

# ── Database Setup ───────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watches (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            watch_type TEXT NOT NULL,         -- 'restaurant' | 'campground' | 'custom'
            name TEXT NOT NULL,               -- friendly name
            url TEXT NOT NULL,                -- URL to monitor
            target_date TEXT,                 -- desired date (YYYY-MM-DD) — legacy, use date_from/date_to
            date_from TEXT,                   -- start of desired date range (YYYY-MM-DD)
            date_to TEXT,                     -- end of desired date range (YYYY-MM-DD)
            target_time TEXT,                 -- desired time for restaurants
            party_size INTEGER DEFAULT 2,
            site_numbers TEXT,                -- comma-separated campsite numbers to watch
            check_pattern TEXT,               -- CSS selector or text pattern to look for
            notify_via TEXT DEFAULT 'email',  -- 'email' | 'sms' | 'both'
            phone TEXT,
            status TEXT DEFAULT 'active',     -- 'active' | 'paused' | 'found' | 'expired'
            last_checked_at TEXT,
            last_result TEXT,                 -- 'available' | 'unavailable' | 'error'
            last_result_detail TEXT,          -- human-readable detail of last check
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY,
            watch_id TEXT NOT NULL,
            message TEXT NOT NULL,
            alert_type TEXT DEFAULT 'availability',
            sent_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (watch_id) REFERENCES watches(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_tokens (
            token TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            token_type TEXT NOT NULL,        -- 'magic_link' | 'session'
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS check_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            watch_id TEXT NOT NULL,
            checked_at TEXT DEFAULT (datetime('now')),
            result TEXT NOT NULL,             -- 'available' | 'unavailable' | 'error'
            details TEXT,
            response_time_ms INTEGER,
            FOREIGN KEY (watch_id) REFERENCES watches(id)
        )
    """)
    conn.commit()
    conn.close()
    print(f"[DB] Database initialized at {DB_PATH}")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Monitoring Engine ────────────────────────────────────────────────────────

class MonitorEngine:
    """Periodically checks watched URLs for availability changes."""

    def __init__(self):
        self.running = False
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        print(f"[Monitor] Started — checking every {CHECK_INTERVAL_SECONDS}s")

    def stop(self):
        self.running = False

    def _run_loop(self):
        while self.running:
            try:
                self._check_all_watches()
            except Exception as e:
                print(f"[Monitor] Error in check loop: {e}")
            time.sleep(CHECK_INTERVAL_SECONDS)

    def _check_all_watches(self):
        conn = get_db()
        watches = conn.execute(
            "SELECT * FROM watches WHERE status = 'active'"
        ).fetchall()
        conn.close()

        for watch in watches:
            try:
                self._check_single(dict(watch))
            except Exception as e:
                print(f"[Monitor] Error checking {watch['name']}: {e}")

    def _check_single(self, watch):
        start = time.time()
        available = False
        details = ""

        try:
            # Fetch the page
            req = urllib.request.Request(
                watch["url"],
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/120.0.0.0 Safari/537.36"
                }
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")

            # Check based on watch type
            if watch["watch_type"] == "restaurant":
                available, details = self._check_restaurant(html, watch)
            elif watch["watch_type"] == "campground":
                available, details = self._check_campground(html, watch)
            else:
                available, details = self._check_custom(html, watch)

            # If availability was found, verify dates on page fall within the desired range
            if available and (watch.get("date_from") or watch.get("date_to")):
                in_range, date_details = self._check_date_range(html, watch)
                if not in_range:
                    available = False
                    details = date_details

        except urllib.error.HTTPError as e:
            details = f"HTTP {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            details = f"Connection error: {e.reason}"
        except Exception as e:
            details = f"Error: {str(e)}"

        elapsed_ms = int((time.time() - start) * 1000)
        result = "available" if available else ("error" if "error" in details.lower() or "Error" in details else "unavailable")

        # Log the check
        conn = get_db()
        conn.execute(
            "INSERT INTO check_log (watch_id, result, details, response_time_ms) VALUES (?, ?, ?, ?)",
            (watch["id"], result, details, elapsed_ms)
        )
        conn.execute(
            "UPDATE watches SET last_checked_at = datetime('now'), last_result = ?, last_result_detail = ?, updated_at = datetime('now') WHERE id = ?",
            (result, details, watch["id"])
        )
        conn.commit()

        # Send alert if availability found
        if available:
            alert_id = str(uuid.uuid4())
            message = f"🎉 Availability found for {watch['name']}! {details}"
            conn.execute(
                "INSERT INTO alerts (id, watch_id, message) VALUES (?, ?, ?)",
                (alert_id, watch["id"], message)
            )
            conn.execute(
                "UPDATE watches SET status = 'found', updated_at = datetime('now') WHERE id = ?",
                (watch["id"],)
            )
            conn.commit()
            self._send_notification(watch, message)
            print(f"[Monitor] ✅ FOUND: {watch['name']} — {details}")
        else:
            print(f"[Monitor] ❌ {watch['name']} — {details} ({elapsed_ms}ms)")

        conn.close()

    def _check_restaurant(self, html, watch):
        """Check restaurant reservation availability."""
        # Look for common availability indicators
        patterns = [
            r"available",
            r"reserve",
            r"book\s+now",
            r"open\s+table",
            r"select\s+a?\s*time",
        ]
        # Check if a custom pattern was provided
        if watch.get("check_pattern"):
            patterns = [watch["check_pattern"]]

        html_lower = html.lower()
        for pattern in patterns:
            if re.search(pattern, html_lower):
                # Also check that "no availability" type messages aren't present
                negative_patterns = [
                    r"no\s+availability",
                    r"fully\s+booked",
                    r"sold\s+out",
                    r"no\s+tables",
                    r"waitlist\s+only",
                    r"no\s+reservations?\s+available",
                ]
                for neg in negative_patterns:
                    if re.search(neg, html_lower):
                        return False, "Page indicates no availability"
                return True, f"Availability indicator found (pattern: {pattern})"

        return False, "No availability indicators found"

    def _check_campground(self, html, watch):
        """Check campground/park reservation availability."""
        html_lower = html.lower()

        # If specific site numbers are requested, check for those first
        site_numbers = watch.get("site_numbers")
        if site_numbers:
            sites = [s.strip() for s in site_numbers.split(",") if s.strip()]
            found_sites = []
            for site in sites:
                # Look for the site number near availability indicators
                # Common patterns: "Site 42 Available", "Site #42", "#42 - Available", "042"
                site_patterns = [
                    rf'(?:site|campsite|space|spot)\s*#?\s*0*{re.escape(site)}\b[^<]{{0,100}}(?:available|open|book|reserve)',
                    rf'(?:available|open|book|reserve)[^<]{{0,100}}(?:site|campsite|space|spot)\s*#?\s*0*{re.escape(site)}\b',
                    rf'#\s*0*{re.escape(site)}\b[^<]{{0,100}}(?:available|open|book|reserve)',
                    rf'\b0*{re.escape(site)}\b[^<]{{0,200}}(?:available|open|book\s+now|reserve)',
                ]
                for sp in site_patterns:
                    if re.search(sp, html_lower):
                        found_sites.append(site)
                        break

            if found_sites:
                return True, f"Site(s) {', '.join(found_sites)} appear available!"
            # If specific sites requested but none found available, still check general availability
            # but report that the specific sites weren't found

        # General availability check
        positive_patterns = [
            r"available",
            r"book\s+now",
            r"reserve\s+(?:this\s+)?site",
            r"open\s+sites?",
            r"select\s+site",
        ]

        if watch.get("check_pattern"):
            positive_patterns = [watch["check_pattern"]]

        negative_patterns = [
            r"no\s+sites?\s+available",
            r"fully?\s+reserved",
            r"sold\s+out",
            r"no\s+availability",
            r"all\s+sites?\s+(?:are\s+)?reserved",
        ]

        for neg in negative_patterns:
            if re.search(neg, html_lower):
                if site_numbers:
                    return False, f"Site(s) {site_numbers} not found available — page shows fully reserved"
                return False, "Site indicates fully reserved"

        for pattern in positive_patterns:
            if re.search(pattern, html_lower):
                if site_numbers:
                    return True, f"General availability found (specific sites {site_numbers} not confirmed — check manually)"
                return True, f"Availability indicator found (pattern: {pattern})"

        if site_numbers:
            return False, f"Site(s) {site_numbers} not found available"
        return False, "No availability indicators found"

    def _check_custom(self, html, watch):
        """Check custom URL with user-provided pattern."""
        if not watch.get("check_pattern"):
            return False, "No check pattern configured"

        if re.search(watch["check_pattern"], html, re.IGNORECASE):
            return True, f"Pattern matched: {watch['check_pattern']}"
        return False, f"Pattern not found: {watch['check_pattern']}"

    def _check_date_range(self, html, watch):
        """Verify that dates found on the page fall within the watch's date range.
        Returns (is_in_range: bool, detail: str)."""
        date_from_str = watch.get("date_from")
        date_to_str = watch.get("date_to")

        try:
            date_from = datetime.strptime(date_from_str, "%Y-%m-%d") if date_from_str else None
            date_to = datetime.strptime(date_to_str, "%Y-%m-%d") if date_to_str else None
        except ValueError:
            # If dates can't be parsed, don't filter — let it through
            return True, "Date range check skipped (invalid date format)"

        if not date_from and not date_to:
            return True, "No date range set"

        # Look for dates on the page in common formats
        # Matches: 2026-04-15, 04/15/2026, April 15 2026, Apr 15, 2026, etc.
        date_patterns = [
            (r'(\d{4})-(\d{1,2})-(\d{1,2})', '%Y-%m-%d'),           # 2026-04-15
            (r'(\d{1,2})/(\d{1,2})/(\d{4})', '%m/%d/%Y'),           # 04/15/2026
            (r'(\d{1,2})/(\d{1,2})/(\d{2})\b', '%m/%d/%y'),         # 04/15/26
        ]

        # Also look for written-out month names
        month_names = {
            'january': 1, 'february': 2, 'march': 3, 'april': 4,
            'may': 5, 'june': 6, 'july': 7, 'august': 8,
            'september': 9, 'october': 10, 'november': 11, 'december': 12,
            'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
            'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
        }

        found_dates = []

        # Extract numeric format dates
        for pattern, fmt in date_patterns:
            for match in re.finditer(pattern, html):
                try:
                    d = datetime.strptime(match.group(0), fmt)
                    found_dates.append(d)
                except ValueError:
                    continue

        # Extract written month dates like "April 15, 2026" or "Apr 15 2026"
        month_pattern = r'\b(' + '|'.join(month_names.keys()) + r')\s+(\d{1,2})(?:,?\s*(\d{4}))?\b'
        for match in re.finditer(month_pattern, html.lower()):
            try:
                month = month_names[match.group(1)]
                day = int(match.group(2))
                year = int(match.group(3)) if match.group(3) else datetime.now().year
                found_dates.append(datetime(year, month, day))
            except (ValueError, KeyError):
                continue

        if not found_dates:
            # No dates found on page — can't verify, let the availability through
            # but note it in the details
            return True, "Availability found (no dates detected on page to verify range)"

        # Check if any found date falls within the desired range
        dates_in_range = []
        dates_out_of_range = []
        for d in found_dates:
            in_range = True
            if date_from and d < date_from:
                in_range = False
            if date_to and d > date_to + timedelta(days=1):  # Include the end date
                in_range = False
            if in_range:
                dates_in_range.append(d.strftime('%Y-%m-%d'))
            else:
                dates_out_of_range.append(d.strftime('%Y-%m-%d'))

        if dates_in_range:
            unique_dates = sorted(set(dates_in_range))[:5]
            return True, f"Availability found for dates in range: {', '.join(unique_dates)}"
        else:
            unique_out = sorted(set(dates_out_of_range))[:5]
            range_str = f"{date_from_str or '...'} to {date_to_str or '...'}"
            return False, f"Availability found but outside your date range ({range_str}). Dates on page: {', '.join(unique_out)}"

    def _send_notification(self, watch, message):
        """Send email notification via Resend API."""
        if not RESEND_API_KEY:
            print(f"[Notify] Email not configured (no RESEND_API_KEY) — would send to {watch['user_email']}: {message}")
            return

        try:
            # Build detail lines
            detail_lines_html = ""
            date_from = watch.get("date_from") or watch.get("target_date")
            date_to = watch.get("date_to")
            if date_from:
                date_display = f"{date_from} → {date_to}" if date_to and date_to != date_from else date_from
                detail_lines_html += f'<div style="margin:6px 0;font-size:16px;">📅 {date_display}</div>'
            if watch.get("target_time"):
                detail_lines_html += f'<div style="margin:6px 0;font-size:16px;">⏰ {watch["target_time"]}</div>'
            if watch.get("party_size"):
                detail_lines_html += f'<div style="margin:6px 0;font-size:16px;">👥 Party of {watch["party_size"]}</div>'

            html_body = f"""
            <html>
            <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; background: #f4f4f4;">
                <div style="background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
                    <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px; color: white; text-align: center;">
                        <h1 style="margin: 0; font-size: 24px;">🎉 Reservation Available!</h1>
                        <p style="margin: 8px 0 0; opacity: 0.9; font-size: 16px;">{watch['name']}</p>
                    </div>
                    <div style="padding: 24px;">
                        <p style="color: #333; font-size: 16px; margin-top: 0;">Great news! We found availability:</p>
                        <div style="background: #f8f9fa; border-radius: 8px; padding: 16px; margin: 16px 0;">
                            {detail_lines_html}
                            <div style="margin:8px 0 0; font-size:14px; color:#666;">🔗 <a href="{watch['url']}" style="color: #667eea;">{watch['url'][:80]}{'...' if len(watch['url']) > 80 else ''}</a></div>
                        </div>
                        <p style="color: #666; font-size: 14px;">{message}</p>
                        <a href="{watch['url']}" style="display: inline-block; background: #667eea; color: white; padding: 14px 28px; border-radius: 8px; text-decoration: none; font-weight: 600; margin-top: 8px; font-size: 16px;">
                            Book Now →
                        </a>
                        <p style="color: #999; font-size: 13px; margin-top: 20px;">Book fast — these go quickly!</p>
                    </div>
                    <div style="padding: 16px 24px; background: #f8f9fa; border-top: 1px solid #eee; text-align: center;">
                        <p style="color: #aaa; font-size: 12px; margin: 0;">Sent by <a href="https://reservationalert.ai" style="color: #667eea;">ReservationAlert.ai</a></p>
                    </div>
                </div>
            </body>
            </html>
            """

            payload = json.dumps({
                "from": FROM_EMAIL,
                "to": [watch["user_email"]],
                "subject": f"🔔 {watch['name']} — Reservation Available!",
                "html": html_body,
            }).encode("utf-8")

            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=payload,
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )

            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read().decode())
                print(f"[Notify] Email sent to {watch['user_email']} — id: {result.get('id')}")

        except Exception as e:
            print(f"[Notify] Failed to send email: {e}")


# ── API Handler ──────────────────────────────────────────────────────────────

class APIHandler(http.server.SimpleHTTPRequestHandler):

    def __init__(self, *args, monitor=None, **kwargs):
        self.monitor = monitor
        super().__init__(*args, directory=os.path.join(os.path.dirname(__file__), "static"), **kwargs)

    # ── Auth helpers ───────────────────────────────────────────────────────

    def _get_auth_email(self):
        """Validate session token from Authorization header and return user email."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:]
        conn = get_db()
        row = conn.execute(
            "SELECT email, expires_at FROM auth_tokens WHERE token = ? AND token_type = 'session' AND used = 0",
            (token,)
        ).fetchone()
        conn.close()
        if not row:
            return None
        if datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S") < datetime.utcnow():
            return None
        return row["email"]

    def _require_auth(self):
        """Return user email or send 401 and return None."""
        email = self._get_auth_email()
        if not email:
            self._json_response({"error": "Unauthorized — please log in"}, 401)
        return email

    def _send_magic_link_email(self, email, token):
        """Send the magic link login email via Resend."""
        link = f"{BASE_URL}/?token={token}"

        if not RESEND_API_KEY:
            print(f"[Auth] Magic link for {email}: {link}")
            return True

        try:
            html_body = f"""
            <html>
            <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 500px; margin: 0 auto; padding: 20px;">
                <div style="background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
                    <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px; color: white; text-align: center;">
                        <h1 style="margin: 0; font-size: 22px;">🔔 ReservationAlert.ai</h1>
                    </div>
                    <div style="padding: 24px; text-align: center;">
                        <p style="color: #333; font-size: 16px;">Click the button below to sign in:</p>
                        <a href="{link}" style="display: inline-block; background: #667eea; color: white; padding: 14px 32px; border-radius: 8px; text-decoration: none; font-weight: 600; margin: 16px 0; font-size: 16px;">
                            Sign In →
                        </a>
                        <p style="color: #999; font-size: 13px; margin-top: 16px;">This link expires in {MAGIC_LINK_EXPIRY_MINUTES} minutes.</p>
                        <p style="color: #ccc; font-size: 11px; margin-top: 12px;">If you didn't request this, just ignore this email.</p>
                    </div>
                </div>
            </body>
            </html>
            """
            payload = json.dumps({
                "from": FROM_EMAIL,
                "to": [email],
                "subject": "🔔 Sign in to ReservationAlert.ai",
                "html": html_body,
            }).encode("utf-8")

            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=payload,
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read().decode())
                print(f"[Auth] Magic link email sent to {email} — id: {result.get('id')}")
            return True
        except urllib.error.HTTPError as he:
            body = he.read().decode("utf-8", errors="replace")
            print(f"[Auth] Failed to send magic link email: {he} — Response body: {body}")
            print(f"[Auth] API key starts with: {RESEND_API_KEY[:12]}... (len={len(RESEND_API_KEY)})")
            return False
        except Exception as e:
            print(f"[Auth] Failed to send magic link email: {e}")
            return False

    # ── Routes ──────────────────────────────────────────────────────────

    def do_GET(self):
        # Public routes (no auth needed)
        if self.path == "/api/health":
            self._json_response({"status": "ok", "version": "1.0.0"})
        elif self.path.startswith("/api/auth/verify"):
            self._verify_magic_link()
        elif self.path == "/api/auth/me":
            self._auth_me()
        # Protected routes
        elif self.path == "/api/watches":
            self._get_watches()
        elif self.path.startswith("/api/watches/") and "/alerts" in self.path:
            watch_id = self.path.split("/")[3]
            self._get_alerts(watch_id)
        elif self.path.startswith("/api/watches/") and "/logs" in self.path:
            watch_id = self.path.split("/")[3]
            self._get_logs(watch_id)
        elif self.path.startswith("/api/watches/"):
            watch_id = self.path.split("/")[3]
            self._get_watch(watch_id)
        elif self.path == "/api/stats":
            self._get_stats()
        elif self.path == "/api/alerts":
            self._get_all_alerts()
        else:
            # Serve static files; default to index.html
            if self.path == "/" or not os.path.exists(
                os.path.join(os.path.dirname(__file__), "static", self.path.lstrip("/"))
            ):
                self.path = "/index.html"
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/auth/login":
            self._auth_login()
        elif self.path == "/api/watches":
            self._create_watch()
        elif self.path.startswith("/api/watches/") and "/check" in self.path:
            watch_id = self.path.split("/")[3]
            self._trigger_check(watch_id)
        else:
            self._json_response({"error": "Not found"}, 404)

    def do_PUT(self):
        if self.path.startswith("/api/watches/"):
            watch_id = self.path.split("/")[3]
            self._update_watch(watch_id)
        else:
            self._json_response({"error": "Not found"}, 404)

    def do_DELETE(self):
        if self.path.startswith("/api/watches/"):
            watch_id = self.path.split("/")[3]
            self._delete_watch(watch_id)
        else:
            self._json_response({"error": "Not found"}, 404)

    # ── Auth Endpoints ──────────────────────────────────────────────────

    def _auth_login(self):
        """Send a magic link to the user's email."""
        data = self._read_json()
        if not data:
            return
        email = (data.get("email") or "").strip().lower()
        if not email or "@" not in email:
            self._json_response({"error": "Please enter a valid email address"}, 400)
            return

        # Create magic link token
        token = secrets.token_urlsafe(32)
        expires = (datetime.utcnow() + timedelta(minutes=MAGIC_LINK_EXPIRY_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        conn = get_db()
        conn.execute(
            "INSERT INTO auth_tokens (token, email, token_type, expires_at) VALUES (?, ?, 'magic_link', ?)",
            (token, email, expires)
        )
        conn.commit()
        conn.close()

        if self._send_magic_link_email(email, token):
            self._json_response({"ok": True, "message": "Check your email for a sign-in link!"})
        else:
            self._json_response({"error": "Failed to send email. Please try again."}, 500)

    def _verify_magic_link(self):
        """Verify magic link token and return a session token."""
        # Parse token from query string
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(self.path).query)
        token = (query.get("token") or [None])[0]

        if not token:
            self._json_response({"error": "Missing token"}, 400)
            return

        conn = get_db()
        row = conn.execute(
            "SELECT email, expires_at, used FROM auth_tokens WHERE token = ? AND token_type = 'magic_link'",
            (token,)
        ).fetchone()

        if not row:
            conn.close()
            self._json_response({"error": "Invalid or expired link"}, 401)
            return

        if row["used"]:
            conn.close()
            self._json_response({"error": "This link has already been used. Please request a new one."}, 401)
            return

        if datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S") < datetime.utcnow():
            conn.close()
            self._json_response({"error": "This link has expired. Please request a new one."}, 401)
            return

        # Mark magic link as used
        conn.execute("UPDATE auth_tokens SET used = 1 WHERE token = ?", (token,))

        # Create session token
        session_token = secrets.token_urlsafe(48)
        session_expires = (datetime.utcnow() + timedelta(days=SESSION_EXPIRY_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO auth_tokens (token, email, token_type, expires_at) VALUES (?, ?, 'session', ?)",
            (session_token, row["email"], session_expires)
        )
        conn.commit()
        conn.close()

        self._json_response({
            "ok": True,
            "session_token": session_token,
            "email": row["email"],
        })

    def _auth_me(self):
        """Return current user info if authenticated."""
        email = self._get_auth_email()
        if email:
            self._json_response({"email": email})
        else:
            self._json_response({"error": "Not authenticated"}, 401)

    # ── API Endpoints ────────────────────────────────────────────────────────

    def _get_watches(self):
        email = self._require_auth()
        if not email:
            return
        conn = get_db()
        watches = conn.execute("SELECT * FROM watches WHERE user_email = ? ORDER BY created_at DESC", (email,)).fetchall()
        conn.close()
        self._json_response([dict(w) for w in watches])

    def _get_watch(self, watch_id):
        email = self._require_auth()
        if not email:
            return
        conn = get_db()
        watch = conn.execute("SELECT * FROM watches WHERE id = ? AND user_email = ?", (watch_id, email)).fetchone()
        conn.close()
        if watch:
            self._json_response(dict(watch))
        else:
            self._json_response({"error": "Watch not found"}, 404)

    def _create_watch(self):
        email = self._require_auth()
        if not email:
            return
        data = self._read_json()
        if not data:
            return

        watch_id = str(uuid.uuid4())
        conn = get_db()
        conn.execute("""
            INSERT INTO watches (id, user_email, watch_type, name, url, target_date, date_from, date_to,
                                 target_time, party_size, site_numbers, check_pattern, notify_via, phone)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            watch_id,
            email,  # Use authenticated email, not form data
            data.get("watch_type", "custom"),
            data.get("name", "Untitled Watch"),
            data.get("url", ""),
            data.get("target_date"),
            data.get("date_from") or data.get("target_date"),
            data.get("date_to") or data.get("target_date"),
            data.get("target_time"),
            data.get("party_size", 2),
            data.get("site_numbers"),
            data.get("check_pattern"),
            data.get("notify_via", "email"),
            data.get("phone"),
        ))
        conn.commit()

        watch = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
        conn.close()
        print(f"[API] Created watch: {data.get('name')} ({watch_id})")
        self._json_response(dict(watch), 201)

    def _update_watch(self, watch_id):
        email = self._require_auth()
        if not email:
            return
        data = self._read_json()
        if not data:
            return

        conn = get_db()
        existing = conn.execute("SELECT * FROM watches WHERE id = ? AND user_email = ?", (watch_id, email)).fetchone()
        if not existing:
            conn.close()
            self._json_response({"error": "Watch not found"}, 404)
            return

        fields = []
        values = []
        for key in ["user_email", "watch_type", "name", "url", "target_date", "date_from", "date_to",
                     "target_time", "party_size", "site_numbers", "check_pattern", "notify_via", "phone", "status", "last_result_detail"]:
            if key in data:
                fields.append(f"{key} = ?")
                values.append(data[key])

        if fields:
            fields.append("updated_at = datetime('now')")
            values.append(watch_id)
            conn.execute(f"UPDATE watches SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()

        watch = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
        conn.close()
        self._json_response(dict(watch))

    def _delete_watch(self, watch_id):
        email = self._require_auth()
        if not email:
            return
        conn = get_db()
        # Verify ownership
        existing = conn.execute("SELECT id FROM watches WHERE id = ? AND user_email = ?", (watch_id, email)).fetchone()
        if not existing:
            conn.close()
            self._json_response({"error": "Watch not found"}, 404)
            return
        conn.execute("DELETE FROM alerts WHERE watch_id = ?", (watch_id,))
        conn.execute("DELETE FROM check_log WHERE watch_id = ?", (watch_id,))
        conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        conn.commit()
        conn.close()
        self._json_response({"deleted": True})

    def _trigger_check(self, watch_id):
        email = self._require_auth()
        if not email:
            return
        conn = get_db()
        watch = conn.execute("SELECT * FROM watches WHERE id = ? AND user_email = ?", (watch_id, email)).fetchone()
        conn.close()
        if not watch:
            self._json_response({"error": "Watch not found"}, 404)
            return

        # Run check in background thread
        if self.monitor:
            threading.Thread(
                target=self.monitor._check_single,
                args=(dict(watch),),
                daemon=True
            ).start()
        self._json_response({"status": "check_triggered"})

    def _get_alerts(self, watch_id):
        conn = get_db()
        alerts = conn.execute(
            "SELECT * FROM alerts WHERE watch_id = ? ORDER BY sent_at DESC LIMIT 50",
            (watch_id,)
        ).fetchall()
        conn.close()
        self._json_response([dict(a) for a in alerts])

    def _get_all_alerts(self):
        email = self._require_auth()
        if not email:
            return
        conn = get_db()
        alerts = conn.execute(
            "SELECT a.*, w.name as watch_name FROM alerts a JOIN watches w ON a.watch_id = w.id WHERE w.user_email = ? ORDER BY a.sent_at DESC LIMIT 100",
            (email,)
        ).fetchall()
        conn.close()
        self._json_response([dict(a) for a in alerts])

    def _get_logs(self, watch_id):
        conn = get_db()
        logs = conn.execute(
            "SELECT * FROM check_log WHERE watch_id = ? ORDER BY checked_at DESC LIMIT 100",
            (watch_id,)
        ).fetchall()
        conn.close()
        self._json_response([dict(l) for l in logs])

    def _get_stats(self):
        email = self._require_auth()
        if not email:
            return
        conn = get_db()
        stats = {
            "total_watches": conn.execute("SELECT COUNT(*) FROM watches WHERE user_email = ?", (email,)).fetchone()[0],
            "active_watches": conn.execute("SELECT COUNT(*) FROM watches WHERE status='active' AND user_email = ?", (email,)).fetchone()[0],
            "found_watches": conn.execute("SELECT COUNT(*) FROM watches WHERE status='found' AND user_email = ?", (email,)).fetchone()[0],
            "total_alerts": conn.execute("SELECT COUNT(*) FROM alerts WHERE watch_id IN (SELECT id FROM watches WHERE user_email = ?)", (email,)).fetchone()[0],
            "total_checks": conn.execute("SELECT COUNT(*) FROM check_log WHERE watch_id IN (SELECT id FROM watches WHERE user_email = ?)", (email,)).fetchone()[0],
            "checks_today": conn.execute(
                "SELECT COUNT(*) FROM check_log WHERE checked_at >= date('now') AND watch_id IN (SELECT id FROM watches WHERE user_email = ?)",
                (email,)
            ).fetchone()[0],
        }
        conn.close()
        self._json_response(stats)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            return json.loads(body)
        except Exception as e:
            self._json_response({"error": f"Invalid JSON: {e}"}, 400)
            return None

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def log_message(self, format, *args):
        if "/api/" in (args[0] if args else ""):
            print(f"[HTTP] {args[0]}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║                                                          ║
    ║   🔔  ReservationAlert.ai  — MVP Server                 ║
    ║                                                          ║
    ║   Monitor reservations. Get notified instantly.          ║
    ║                                                          ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    # Initialize database
    init_db()

    # Start monitoring engine
    monitor = MonitorEngine()
    monitor.start()

    # Create static directory if it doesn't exist
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    os.makedirs(static_dir, exist_ok=True)

    # Start HTTP server
    handler = partial(APIHandler, monitor=monitor)
    server = http.server.HTTPServer(("0.0.0.0", PORT), handler)
    print(f"[Server] Running at http://localhost:{PORT}")
    print(f"[Server] Press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] Shutting down...")
        monitor.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
