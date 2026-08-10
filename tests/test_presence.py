"""Unit tests for pure logic (no Windows API, no Discord connection).

Run:  python -m pytest tests/ -q
"""
import time

import hermes_presence as hp


def sess(**kw):
    base = {
        "started_at": time.time(),
        "msgs": 10,
        "tokens": 15_000,
        "cost": 0.12,
        "last_tool": None,
        "last_msg_ts": time.time(),
    }
    base.update(kw)
    return base


class TestFmtTokens:
    def test_zero(self):
        assert hp.fmt_tokens(0) == "0"

    def test_none(self):
        assert hp.fmt_tokens(None) == "0"

    def test_plain(self):
        assert hp.fmt_tokens(999) == "999"

    def test_thousands(self):
        assert hp.fmt_tokens(1_500) == "1.5k"
        assert hp.fmt_tokens(503_000) == "503.0k"

    def test_millions(self):
        assert hp.fmt_tokens(1_500_000) == "1.5M"


class TestModelRegex:
    def test_standard_line(self):
        line = ("2026-08-10 12:00:00,000 INFO [abc] agent.conversation_loop: "
                "API call #1: model=deepseek/deepseek-v4-flash-0731 provider=openrouter")
        m = hp._LINE_MODEL_RE.search(line)
        assert m
        assert m.group(2) == "deepseek/deepseek-v4-flash-0731"

    def test_format_change_is_not_matched(self):
        # hypothetic new log format: must NOT silently match
        line = "2026-08-10 12:00:00,000 INFO model_id=custom-model-1"
        assert hp._LINE_MODEL_RE.search(line) is None

    def test_last_line_wins(self):
        data = (
            "2026-08-10 12:00:00,000 INFO API call #1: model=a/b in=1\n"
            "2026-08-10 12:01:00,000 INFO API call #2: model=c/d in=2\n"
        )
        model = None
        for line in data.splitlines():
            m = hp._LINE_MODEL_RE.search(line)
            if m:
                model = m.group(2)
        assert model == "c/d"


class TestNormalise:
    def test_new_format(self):
        raw = {
            "hermes_path": r"%LOCALAPPDATA%\hermes",
            "discord_app_id": "123",
            "status_template": "X",
            "poll_interval": 3,
            "stale_after": 60,
        }
        cfg = hp._normalise(raw)
        assert cfg["hermes_path"] == r"%LOCALAPPDATA%\hermes"
        assert cfg["app_id"] == "123"
        assert cfg["status_template"] == "X"
        assert cfg["poll_interval"] == 3
        assert cfg["stale_after"] == 60

    def test_old_format_compat(self):
        raw = {"app_id": "OLD", "paths": {
            "hermes_home": "H", "state_db": "S", "agent_log": "L"}}
        cfg = hp._normalise(raw)
        assert cfg["app_id"] == "OLD"
        assert cfg["hermes_path"] == "H"
        assert cfg["state_db"] == "S"
        assert cfg["agent_log"] == "L"

    def test_discord_app_id_priority(self):
        assert hp._normalise({"app_id": "A", "discord_app_id": "B"})["app_id"] == "B"

    def test_empty(self):
        cfg = hp._normalise({})
        assert cfg["app_id"] == ""
        assert cfg["status_template"] == ""
        assert cfg["poll_interval"] is None


class TestBuildPresence:
    def setup_method(self):
        self._prev = hp.STATUS_TEMPLATE
        hp.STATUS_TEMPLATE = "{action} • {tokens} tok • ${cost}"

    def teardown_method(self):
        hp.STATUS_TEMPLATE = self._prev

    def test_chatting_with_stats(self):
        details, state, timer = hp.build_presence(sess(tokens=1_500), "m/x", time.time())
        assert "Model: m/x" in details
        assert "Chatting with Hermes" in state
        assert "1.5k tok" in state
        assert "$0.12" in state

    def test_running_tool(self):
        s = sess(last_tool=("execute_code", time.time()))
        _, state, _ = hp.build_presence(s, "m/x", time.time())
        assert "Running: python" in state

    def test_idle(self):
        # 700s > CHAT_FRESH_SEC (600) — сообщение старое, но не stale
        s = sess(last_msg_ts=time.time() - 700)
        _, state, _ = hp.build_presence(s, "m/x", time.time())
        assert "Idle in Hermes" in state

    def test_stale(self):
        old = time.time() - 5000
        details, state, _ = hp.build_presence(sess(last_msg_ts=old), "m/x", old)
        assert "stale" in state.lower()
        assert "(stale)" in details

    def test_unknown_model(self):
        details, _, _ = hp.build_presence(sess(), None, None)
        assert "Model: unknown" in details

    def test_bad_template_falls_back(self):
        hp.STATUS_TEMPLATE = "{bogus_field}"
        _, state, _ = hp.build_presence(sess(), "m/x", time.time())
        assert "{bogus_field}" not in state
        assert "Chatting" in state

    def test_custom_template_order(self):
        hp.STATUS_TEMPLATE = "{cost} // {action}"
        _, state, _ = hp.build_presence(sess(), "m/x", time.time())
        assert state.startswith("0.12 // Chatting")