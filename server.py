#!/usr/bin/env python3
"""
ReservationAlert.ai - MVP Backend Server
Zero-dependency Python server (uses only standard library)
"""

import http.server
import json
import sqlite3
import os
import uuid
import threading
import time
import smtplib
import urllib.request
import urllib.error
import re
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from functools import partial

# ── Configuration ────────────────────────────────────────────────────────────

PORT = int(os.environ.get("PORT", 8080))
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "reservationalert.db"))
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL", 300))  # 5 minutes default

# Email config (set via environment variables)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "alerts@reservationalert.ai")

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
            target_date TEXT,                 -- desired date (YYYY-MM-DD)
            target_time TEXT,                 -- desired time for restaurants
            party_size INTEGER DEFAULT 2,
            check_pattern TEXT,               -- CSS selector or text pattern to look for
            notify_via TEXT DEFAULT 'email',  -- 'email' | 'sms' | 'both'
            phone TEXT,
            status TEXT DEFAULT 'active',     -- 'active' | 'paused' | 'found' | 'expired'
            last_checked_at TEXT,
            last_result TEXT,                 -- 'available' | 'unavailable' | 'error'
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
            "UPDATE watches SET last_checked_at = datetime('now'), last_result = ?, updated_at = datetime('now') WHERE id = ?",
            (result, watch["id"])
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

        # Look for available site indicators
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
                return False, "Site indicates fully reserved"

        for pattern in positive_patterns:
            if re.search(pattern, html_lower):
                return True, f"Availability indicator found (pattern: {pattern})"

        return False, "No availability indicators found"

    def _check_custom(self, html, watch):
        """Check custom URL with user-provided pattern."""
        if not watch.get("check_pattern"):
            return False, "No check pattern configured"

        if re.search(watch["check_pattern"], html, re.IGNORECASE):
            return True, f"Pattern matched: {watch['check_pattern']}"
        return False, f"Pattern not found: {watch['check_pattern']}"

    def _send_notification(self, watch, message):
        """Send email notification."""
        if not SMTP_USER or not SMTP_PASS:
            print(f"[Notify] Email not configured — would send to {watch['user_email']}: {message}")
            return

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"🔔 ReservationAlert: {watch['name']} is available!"
            msg["From"] = FROM_EMAIL
            msg["To"] = watch["user_email"]

            html_body = f"""
            <html>
            <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
                <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px; border-radius: 12px; color: white; text-align: center;">
                    <h1 style="margin: 0;">🎉 Reservation Found!</h1>
                </div>
                <div style="padding: 24px; background: #f8f9fa; border-radius: 0 0 12px 12px;">
                    <h2 style="color: #333;">{watch['name']}</h2>
                    <p style="color: #666; font-size: 16px;">{message}</p>
                    <a href="{watch['url']}" style="display: inline-block; background: #667eea; color: white; padding: 12px 24px; border-radius: 8px; text-decoration: none; font-weight: 600; margin-top: 16px;">
                        Book Now →
                    </a>
                    <p style="color: #999; font-size: 12px; margin-top: 24px;">
                        This alert was sent by ReservationAlert.ai
                    </p>
                </div>
            </body>
            </html>
            """
            msg.attach(MIMEText(message, "plain"))
            msg.attach(MIMEText(html_body, "html"))

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(FROM_EMAIL, watch["user_email"], msg.as_string())

            print(f"[Notify] Email sent to {watch['user_email']}")
        except Exception as e:
            print(f"[Notify] Failed to send email: {e}")


# ── API Handler ──────────────────────────────────────────────────────────────

class APIHandler(http.server.SimpleHTTPRequestHandler):

    def __init__(self, *args, monitor=None, **kwargs):
        self.monitor = monitor
        super().__init__(*args, directory=os.path.join(os.path.dirname(__file__), "static"), **kwargs)

    def do_GET(self):
        if self.path == "/api/health":
            self._json_response({"status": "ok", "version": "1.0.0"})
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
        if self.path == "/api/watches":
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

    # ── API Endpoints ────────────────────────────────────────────────────────

    def _get_watches(self):
        conn = get_db()
        watches = conn.execute("SELECT * FROM watches ORDER BY created_at DESC").fetchall()
        conn.close()
        self._json_response([dict(w) for w in watches])

    def _get_watch(self, watch_id):
        conn = get_db()
        watch = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
        conn.close()
        if watch:
            self._json_response(dict(watch))
        else:
            self._json_response({"error": "Watch not found"}, 404)

    def _create_watch(self):
        data = self._read_json()
        if not data:
            return

        watch_id = str(uuid.uuid4())
        conn = get_db()
        conn.execute("""
            INSERT INTO watches (id, user_email, watch_type, name, url, target_date, target_time,
                                 party_size, check_pattern, notify_via, phone)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            watch_id,
            data.get("user_email", ""),
            data.get("watch_type", "custom"),
            data.get("name", "Untitled Watch"),
            data.get("url", ""),
            data.get("target_date"),
            data.get("target_time"),
            data.get("party_size", 2),
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
        data = self._read_json()
        if not data:
            return

        conn = get_db()
        existing = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
        if not existing:
            conn.close()
            self._json_response({"error": "Watch not found"}, 404)
            return

        fields = []
        values = []
        for key in ["user_email", "watch_type", "name", "url", "target_date", "target_time",
                     "party_size", "check_pattern", "notify_via", "phone", "status"]:
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
        conn = get_db()
        conn.execute("DELETE FROM alerts WHERE watch_id = ?", (watch_id,))
        conn.execute("DELETE FROM check_log WHERE watch_id = ?", (watch_id,))
        conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        conn.commit()
        conn.close()
        self._json_response({"deleted": True})

    def _trigger_check(self, watch_id):
        conn = get_db()
        watch = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
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
        conn = get_db()
        alerts = conn.execute(
            "SELECT a.*, w.name as watch_name FROM alerts a JOIN watches w ON a.watch_id = w.id ORDER BY a.sent_at DESC LIMIT 100"
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
        conn = get_db()
        stats = {
            "total_watches": conn.execute("SELECT COUNT(*) FROM watches").fetchone()[0],
            "active_watches": conn.execute("SELECT COUNT(*) FROM watches WHERE status='active'").fetchone()[0],
            "found_watches": conn.execute("SELECT COUNT(*) FROM watches WHERE status='found'").fetchone()[0],
            "total_alerts": conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0],
            "total_checks": conn.execute("SELECT COUNT(*) FROM check_log").fetchone()[0],
            "checks_today": conn.execute(
                "SELECT COUNT(*) FROM check_log WHERE checked_at >= date('now')"
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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
