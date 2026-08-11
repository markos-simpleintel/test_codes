import json
import socket
import threading
import time

from .config import (
    AMI_DETECT_TRANSFER,
    AMI_JANE_MATCH,
    AMI_ORBITI_MATCH,
    AMI_TRANSFER_MATCH,
    AMI_TRANSFER_CONTEXT_PREFIX_LIST,
    AMI_TRANSFER_DIAL_TARGET_LIST,
    CALLER_USER,
)
class AmiReadyEvents:
    def __init__(self, host, port, username, secret, ready_event_name, caller_filter="", trace=False):
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self.ready_event_name = ready_event_name
        self.caller_filter = caller_filter.strip()
        self.trace = trace

        self._sock = None
        self._thread = None
        self._stop_evt = threading.Event()
        self._cond = threading.Condition()
        self._login_cond = threading.Condition()
        self._sequence = 0
        self._last_event = None
        self._ready_events = []
        self._transfer_sequence = 0
        self._last_transfer_event = None
        self._transfer_events = []
        self._channel_sequence = 0
        self._channel_events = []
        self._claimed_channel_keys = set()
        self._identity_linkedids = set()
        self._login_response = None
        self._running = False

    def start(self):
        if not self.username or not self.secret:
            raise ValueError("AMI_USER and AMI_SECRET are required when USE_AMI_READY_EVENTS=1")

        self._sock = socket.create_connection((self.host, self.port), timeout=5)
        self._sock.settimeout(1.0)

        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()
        self._send_action(
            {
                "Action": "Login",
                "Username": self.username,
                "Secret": self.secret,
                "Events": "on",
            }
        )

        response = self._wait_for_login_response(timeout_secs=5)
        if response is None:
            raise RuntimeError("AMI login timed out before any Response message")

        status = response.get("Response", "")
        message = response.get("Message", "")
        if status.lower() != "success":
            raise RuntimeError(f"AMI login failed: Response={status} Message={message}")

        with self._cond:
            self._running = True
            self._cond.notify_all()

        print(f"*** AMI login accepted: {message or status}")

    def stop(self):
        self._stop_evt.set()
        try:
            if self._sock is not None:
                self._send_action({"Action": "Logoff"})
        except Exception:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        with self._cond:
            self._running = False
            self._cond.notify_all()

    def is_running(self):
        with self._cond:
            return self._running

    def current_sequence(self):
        with self._cond:
            return self._sequence

    def current_transfer_sequence(self):
        with self._cond:
            return self._transfer_sequence

    def current_channel_sequence(self):
        with self._cond:
            return self._channel_sequence

    def wait_for_event_after(self, sequence, timeout_secs, stop_evt, linkedid=""):
        deadline = time.time() + timeout_secs if timeout_secs else None
        with self._cond:
            while not stop_evt.is_set():
                for event_sequence, event in self._ready_events:
                    if event_sequence <= sequence:
                        continue
                    if linkedid and event.get("Linkedid") != linkedid:
                        continue
                    return True

                if not self._running:
                    return False

                wait_secs = 0.25
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        return False
                    wait_secs = min(wait_secs, remaining)

                self._cond.wait(wait_secs)
        return False

    def wait_for_transfer_after(self, sequence, timeout_secs, stop_evt, linkedid=""):
        deadline = time.time() + timeout_secs if timeout_secs else None
        with self._cond:
            while not stop_evt.is_set():
                for event_sequence, event in self._transfer_events:
                    if event_sequence <= sequence:
                        continue
                    if linkedid and event.get("Linkedid") != linkedid:
                        continue
                    return dict(event)

                if not self._running:
                    return None

                wait_secs = 0.25
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        return None
                    wait_secs = min(wait_secs, remaining)

                self._cond.wait(wait_secs)
        return None

    def wait_for_channel_after(self, sequence, timeout_secs, stop_evt, expected_caller=""):
        deadline = time.time() + timeout_secs if timeout_secs else None
        with self._cond:
            while not stop_evt.is_set():
                for event_sequence, event in self._channel_events:
                    if event_sequence <= sequence:
                        continue
                    if not self._matches_expected_caller(event, expected_caller):
                        continue

                    channel_key = self._channel_key(event)
                    if not channel_key or channel_key in self._claimed_channel_keys:
                        continue

                    self._claimed_channel_keys.add(channel_key)
                    return dict(event)

                if not self._running:
                    return None

                wait_secs = 0.25
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        return None
                    wait_secs = min(wait_secs, remaining)

                self._cond.wait(wait_secs)
        return None

    def _wait_for_login_response(self, timeout_secs):
        deadline = time.time() + timeout_secs
        with self._login_cond:
            while self._login_response is None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._login_cond.wait(remaining)
            return dict(self._login_response)

    def last_event_summary(self):
        with self._cond:
            if not self._last_event:
                return "<none>"
            return self._summarize_event(self._last_event)

    def last_transfer_event_summary(self):
        with self._cond:
            if not self._last_transfer_event:
                return "<none>"
            return self._summarize_event(self._last_transfer_event)

    def summarize_event(self, msg):
        return self._summarize_event(msg)

    def _send_action(self, fields):
        data = "".join(f"{key}: {value}\r\n" for key, value in fields.items()) + "\r\n"
        self._sock.sendall(data.encode("utf-8"))

    def _reader_loop(self):
        buf = b""
        try:
            while not self._stop_evt.is_set():
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    continue

                if not chunk:
                    break

                buf += chunk
                while b"\r\n\r\n" in buf:
                    raw_msg, buf = buf.split(b"\r\n\r\n", 1)
                    msg = self._parse_message(raw_msg)
                    if not msg:
                        continue
                    self._handle_message(msg)
        except Exception as e:
            print(f"*** AMI listener stopped: {e}")
        finally:
            with self._cond:
                self._running = False
                self._cond.notify_all()

    def _parse_message(self, raw_msg):
        text = raw_msg.decode("utf-8", errors="replace")
        msg = {}
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            msg[key.strip()] = value.strip()
        return msg

    def _handle_message(self, msg):
        response = msg.get("Response")
        if response:
            if self.trace:
                print(f"*** AMI response: {self._summarize_response(msg)}")
            with self._login_cond:
                if self._login_response is None:
                    self._login_response = dict(msg)
                    self._login_cond.notify_all()
            return

        event = msg.get("Event")
        if not event:
            return

        self._log_test_call_identity(msg)

        if self.trace:
            print(f"*** AMI event: {self._summarize_event(msg)}")

        ready_matched = self._matches_ready_event(msg)
        transfer_matched = self._matches_transfer_event(msg)
        channel_matched = self._matches_channel_event(msg)

        if not ready_matched and not transfer_matched and not channel_matched:
            return

        summary = self._summarize_event(msg)

        with self._cond:
            if channel_matched:
                self._channel_sequence += 1
                self._channel_events.append((self._channel_sequence, dict(msg)))
            if ready_matched:
                self._sequence += 1
                self._last_event = dict(msg)
                self._ready_events.append((self._sequence, dict(msg)))
            if transfer_matched:
                self._transfer_sequence += 1
                self._last_transfer_event = dict(msg)
                self._transfer_events.append((self._transfer_sequence, dict(msg)))
            self._cond.notify_all()

        if ready_matched:
            print(f"*** AMI ready event matched: {summary}")
        if transfer_matched:
            print(f"*** AMI transfer event matched: {summary}")

    def _matches_channel_event(self, msg):
        event_name = msg.get("Event", "")
        return event_name.lower() == "newchannel"

    def _log_test_call_identity(self, msg):
        event_name = msg.get("Event", "")
        if event_name.lower() not in {
            "newchannel", "newcallerid", "newconnectedline", "dialbegin", "newexten"
        }:
            return

        linkedid = msg.get("Linkedid", "") or msg.get("Uniqueid", "")
        expected_caller = self.caller_filter or CALLER_USER
        is_initial_test_event = self._matches_expected_caller(msg, expected_caller)
        if is_initial_test_event and linkedid:
            self._identity_linkedids.add(linkedid)
        if not is_initial_test_event and linkedid not in self._identity_linkedids:
            return

        searchable = " ".join(
            msg.get(field, "")
            for field in (
                "Context", "Channel", "DestChannel", "Application", "AppData",
                "Exten", "DialString",
            )
        ).lower()
        stage = "AMI/unknown"
        for label, tokens in (
            ("Orbiti", AMI_ORBITI_MATCH),
            ("Jane", AMI_JANE_MATCH),
            ("Transfer", AMI_TRANSFER_MATCH),
        ):
            if any(token.lower() in searchable for token in tokens):
                stage = label
                break

        record = {
            "stage": stage,
            "event": event_name,
            "caller_number": msg.get("CallerIDNum") or msg.get("Caller", ""),
            "caller_name": msg.get("CallerIDName", ""),
            "connected_number": msg.get("ConnectedLineNum", ""),
            "destination_number": (
                msg.get("DialString") or msg.get("Exten") or msg.get("AppData", "")
            ),
            "context": msg.get("Context", ""),
            "channel": msg.get("Channel", ""),
            "destination_channel": msg.get("DestChannel", ""),
            "uniqueid": msg.get("Uniqueid", ""),
            "linkedid": msg.get("Linkedid", ""),
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        print("[TEST CALL] " + json.dumps(record, sort_keys=True))

    def _channel_key(self, msg):
        return msg.get("Uniqueid") or msg.get("Channel") or msg.get("Linkedid")

    def _matches_expected_caller(self, msg, expected_caller):
        if not expected_caller:
            return True

        fields = (
            "Caller",
            "CallerIDNum",
            "CallerIDName",
            "Channel",
            "Exten",
        )
        return any(expected_caller in msg.get(field, "") for field in fields)

    def _matches_ready_event(self, msg):
        event_name = msg.get("Event", "")
        event_name_lower = event_name.lower()

        if event_name_lower == "userevent" and msg.get("UserEvent") == self.ready_event_name:
            return self._matches_caller_filter(msg)

        if event_name == self.ready_event_name:
            return self._matches_caller_filter(msg)

        return False

    def _matches_transfer_event(self, msg):
        if not AMI_DETECT_TRANSFER:
            return False

        event_name = msg.get("Event", "")
        event_name_lower = event_name.lower()

        if event_name_lower == "newexten":
            context = msg.get("Context", "")
            application = msg.get("Application", "")
            app_data = msg.get("AppData", "")

            if any(context.lower().startswith(prefix.lower()) for prefix in AMI_TRANSFER_CONTEXT_PREFIX_LIST):
                return self._matches_caller_filter(msg)

            if application.lower() == "dial":
                if any(target.lower() in app_data.lower() for target in AMI_TRANSFER_DIAL_TARGET_LIST):
                    return self._matches_caller_filter(msg)

        if event_name_lower == "dialbegin":
            dial_string = " ".join(
                msg.get(field, "")
                for field in ("DialString", "DestChannel", "Destination", "Channel")
            )
            if any(target.lower() in dial_string.lower() for target in AMI_TRANSFER_DIAL_TARGET_LIST):
                return self._matches_caller_filter(msg)

        return False

    def _matches_caller_filter(self, msg):
        if not self.caller_filter:
            return True

        fields = (
            "Caller",
            "CallerIDNum",
            "CallerIDName",
            "ConnectedLineNum",
            "ConnectedLineName",
            "Channel",
            "DestChannel",
            "Exten",
            "Uniqueid",
            "Linkedid",
        )
        return any(self.caller_filter in msg.get(field, "") for field in fields)

    def _summarize_event(self, msg):
        fields = []
        for key in (
            "Event",
            "UserEvent",
            "Command",
            "Application",
            "AppData",
            "Context",
            "DialString",
            "DestChannel",
            "CallerIDNum",
            "CallerIDName",
            "Channel",
            "Exten",
            "Uniqueid",
            "Linkedid",
        ):
            value = msg.get(key)
            if value:
                fields.append(f"{key}={value}")
        return " ".join(fields) if fields else repr(msg)

    def _summarize_response(self, msg):
        fields = []
        for key in ("Response", "Message", "ActionID"):
            value = msg.get(key)
            if value:
                fields.append(f"{key}={value}")
        return " ".join(fields) if fields else repr(msg)



