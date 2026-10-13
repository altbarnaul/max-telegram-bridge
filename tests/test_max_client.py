"""Tests for MAX WebSocket client — comprehensive coverage of all v1 bugs."""
import pytest

from max_bridge.max_client import MaxClient, OpCode


class TestOpCodes:
    """OpCode enum constants."""
    def test_opcodes_defined(self):
        assert OpCode.HEARTBEAT == 1
        assert OpCode.PONG == 5
        assert OpCode.HANDSHAKE == 6
        assert OpCode.AUTH_SNAPSHOT == 19
        assert OpCode.MESSAGE == 64
        assert OpCode.MESSAGE_STATUS == 65
        assert OpCode.DISPATCH == 128
        assert OpCode.PRESENCE == 132


class TestMaxClientInit:
    def test_init(self):
        client = MaxClient(token="tok", device_id="dev")
        assert client.token == "tok"
        assert client.device_id == "dev"
        assert client.my_id is None
        assert client.contacts == {}
        assert client.chat_types == {}
        assert client.chat_titles == {}
        assert client._seq == 0


class TestSendPayloadFormat:
    """Bug #1: Messages must use type=USER with cid and attaches."""
    def test_send_payload_has_user_type(self):
        client = MaxClient(token="t", device_id="d")
        payload = client._build_send_payload(200, "Hello")
        assert payload["message"]["type"] == "USER"

    def test_send_payload_has_cid(self):
        client = MaxClient(token="t", device_id="d")
        payload = client._build_send_payload(200, "Hello")
        assert "cid" in payload["message"]
        assert payload["message"]["cid"] < 0  # negative timestamp

    def test_send_payload_has_attaches(self):
        client = MaxClient(token="t", device_id="d")
        payload = client._build_send_payload(200, "Hello")
        assert payload["message"]["attaches"] == []

    def test_send_payload_full_structure(self):
        client = MaxClient(token="t", device_id="d")
        payload = client._build_send_payload(12345, "Hi there")
        assert payload["chatId"] == 12345
        assert payload["message"]["text"] == "Hi there"
        assert payload["message"]["type"] == "USER"
        assert isinstance(payload["message"]["cid"], int)
        assert payload["message"]["cid"] < 0
        assert payload["message"]["attaches"] == []


class TestContactExtraction:
    """Bug #2: Full name from firstName+lastName. Bug #3: participants is dict."""
    def test_extract_full_name_from_first_last(self):
        client = MaxClient(token="t", device_id="d")
        snapshot = {
            "profile": {"id": 100},
            "contacts": [{
                "id": 300,
                "names": [{"firstName": "Елена", "lastName": "Морозова", "type": "PHONE"}],
            }],
            "chats": [],
        }
        contacts = client.extract_contacts(snapshot)
        assert contacts[300] == "Елена Морозова"

    def test_participants_as_dict_no_crash(self):
        """participants is {userId_str: timestamp}, NOT a list."""
        client = MaxClient(token="t", device_id="d")
        client.my_id = 100
        snapshot = {
            "profile": {"id": 100},
            "contacts": [],
            "chats": [{"chatId": 200, "participants": {"100": 1710000000, "300": 1710000000}, "title": "Chat"}],
        }
        # Must not raise KeyError: 0
        contacts = client.extract_contacts(snapshot)
        assert isinstance(contacts, dict)

    def test_fallback_to_display_name(self):
        client = MaxClient(token="t", device_id="d")
        snapshot = {
            "profile": {"id": 100},
            "contacts": [{"id": 400, "names": [], "displayName": "Guest User"}],
            "chats": [],
        }
        contacts = client.extract_contacts(snapshot)
        assert contacts[400] == "Guest User"

    def test_extract_from_chat_participants_dict(self):
        client = MaxClient(token="t", device_id="d")
        client.my_id = 100
        snapshot = {
            "profile": {"id": 100},
            "contacts": [],
            "chats": [{
                "chatId": 200,
                "participants": {
                    "100": {"name": "Me", "displayName": "My Name"},
                    "300": {"name": "Alice", "displayName": "Alice Smith"},
                },
            }],
        }
        contacts = client.extract_contacts(snapshot)
        assert contacts[100] == "Me"
        assert contacts[300] == "Alice"

    def test_chat_title_as_fallback(self):
        client = MaxClient(token="t", device_id="d")
        client.my_id = 100
        snapshot = {
            "profile": {"id": 100},
            "contacts": [],
            "chats": [{"chatId": 500, "title": "Group Chat", "participants": {}}],
        }
        contacts = client.extract_contacts(snapshot)
        assert contacts[500] == "Group Chat"


class TestAttachmentParsing:
    """Bug #5: Stickers/media must produce placeholder text."""
    def test_sticker_placeholder(self):
        client = MaxClient(token="t", device_id="d")
        payload = {
            "chatId": 200,
            "message": {
                "sender": 300, "id": "m1", "time": 123, "text": "",
                "attaches": [{"_type": "STICKER", "tags": ["😎", "💃🏻"]}],
            },
        }
        msg = client._parse_message(payload)
        assert "Стикер" in msg["text"]
        assert "😎" in msg["text"]

    def test_image_placeholder(self):
        client = MaxClient(token="t", device_id="d")
        payload = {
            "chatId": 200,
            "message": {"sender": 300, "id": "m1", "time": 123, "text": "", "attaches": [{"_type": "IMAGE"}]},
        }
        msg = client._parse_message(payload)
        assert "Изображение" in msg["text"]

    def test_text_plus_attachment(self):
        client = MaxClient(token="t", device_id="d")
        payload = {
            "chatId": 200,
            "message": {
                "sender": 300, "id": "m1", "time": 123, "text": "Look at this",
                "attaches": [{"_type": "IMAGE"}],
            },
        }
        msg = client._parse_message(payload)
        assert "Look at this" in msg["text"]
        assert "Изображение" in msg["text"]

    def test_voice_file_video_placeholders(self):
        client = MaxClient(token="t", device_id="d")
        for att_type, expected in [("VOICE", "Голосовое"), ("VIDEO", "Видео"), ("AUDIO", "Аудио")]:
            payload = {
                "chatId": 200,
                "message": {"sender": 300, "id": "m1", "time": 123, "text": "", "attaches": [{"_type": att_type}]},
            }
            msg = client._parse_message(payload)
            assert expected in msg["text"]

    def test_file_with_name(self):
        client = MaxClient(token="t", device_id="d")
        payload = {
            "chatId": 200,
            "message": {
                "sender": 300, "id": "m1", "time": 123, "text": "",
                "attaches": [{"_type": "FILE", "name": "report.pdf"}],
            },
        }
        msg = client._parse_message(payload)
        assert "report.pdf" in msg["text"]

    def test_empty_text_with_attachment(self):
        """When text is empty, attachment placeholder becomes the text."""
        client = MaxClient(token="t", device_id="d")
        payload = {
            "chatId": 200,
            "message": {"sender": 300, "id": "m1", "time": 123, "text": "", "attaches": [{"_type": "VIDEO"}]},
        }
        msg = client._parse_message(payload)
        assert msg["text"] == "[Видео]"

    def test_multiple_attachments(self):
        client = MaxClient(token="t", device_id="d")
        payload = {
            "chatId": 200,
            "message": {
                "sender": 300, "id": "m1", "time": 123, "text": "Check these out",
                "attaches": [{"_type": "IMAGE"}, {"_type": "VIDEO"}],
            },
        }
        msg = client._parse_message(payload)
        assert "Check these out" in msg["text"]
        assert "[Изображение]" in msg["text"]
        assert "[Видео]" in msg["text"]


class TestChatTypeExtraction:
    def test_extract_chat_types_from_snapshot(self):
        client = MaxClient(token="t", device_id="d")
        snapshot = {
            "profile": {"id": 100},
            "contacts": [],
            "chats": [
                {"chatId": 200, "type": "DIALOG", "title": "", "participants": {"100": 1, "300": 1}},
                {"chatId": 201, "type": "GROUP", "title": "Work Chat", "participants": {"100": 1}},
                {"chatId": 202, "type": "CHANNEL", "title": "News", "participants": {}},
            ],
        }
        client.extract_snapshot_data(snapshot)
        assert client.chat_types[200] == "DIALOG"
        assert client.chat_types[201] == "GROUP"
        assert client.chat_types[202] == "CHANNEL"
        assert client.chat_titles[201] == "Work Chat"
        assert client.chat_titles[202] == "News"

    def test_extract_empty_chats(self):
        """Empty chats array should not crash."""
        client = MaxClient(token="t", device_id="d")
        snapshot = {"profile": {"id": 100}, "contacts": [], "chats": []}
        client.extract_snapshot_data(snapshot)
        assert client.chat_types == {}
        assert client.chat_titles == {}

    def test_extract_chats_without_type(self):
        """Chats without type field should be skipped."""
        client = MaxClient(token="t", device_id="d")
        snapshot = {
            "profile": {"id": 100},
            "contacts": [],
            "chats": [{"chatId": 300, "title": "No Type", "participants": {}}],
        }
        client.extract_snapshot_data(snapshot)
        assert 300 not in client.chat_types
        assert client.chat_titles[300] == "No Type"

    def test_extract_snapshot_also_extracts_contacts(self):
        """extract_snapshot_data should call extract_contacts internally."""
        client = MaxClient(token="t", device_id="d")
        snapshot = {
            "profile": {"id": 100},
            "contacts": [{"id": 400, "names": [{"firstName": "Bob", "lastName": ""}]}],
            "chats": [{"chatId": 200, "type": "DIALOG", "title": "DM", "participants": {}}],
        }
        client.extract_snapshot_data(snapshot)
        assert client.contacts[400] == "Bob"
        assert client.chat_types[200] == "DIALOG"


class TestFrameBuilding:
    def test_build_frame(self):
        client = MaxClient(token="tok", device_id="dev")
        frame = client._build_frame(OpCode.HEARTBEAT, {"interactive": False})
        assert frame == {
            "ver": 11,
            "cmd": 0,
            "seq": 0,
            "opcode": 1,
            "payload": {"interactive": False},
        }
        assert client._seq == 1

    def test_build_frame_increments_seq(self):
        client = MaxClient(token="tok", device_id="dev")
        client._build_frame(OpCode.HEARTBEAT, {})
        client._build_frame(OpCode.HEARTBEAT, {})
        frame = client._build_frame(OpCode.HEARTBEAT, {})
        assert frame["seq"] == 2
        assert client._seq == 3


class TestAuthHandling:
    """Bug #6: Auth error detection."""
    @pytest.mark.asyncio
    async def test_auth_error_detected(self):
        """Auth failure (cmd != 1) should be logged and not proceed."""
        client = MaxClient(token="bad_token", device_id="dev")
        auth_error_frame = {
            "ver": 11,
            "cmd": 3,  # cmd != 1 means auth failed
            "seq": 1,
            "opcode": OpCode.AUTH_SNAPSHOT,
            "payload": {"error": "Invalid token"},
        }
        # Should not crash, just log error
        await client._handle_frame(auth_error_frame)
        # my_id should not be set
        assert client.my_id is None

    @pytest.mark.asyncio
    async def test_auth_success(self):
        """Auth success (cmd == 1) should set my_id."""
        client = MaxClient(token="tok", device_id="dev")
        auth_success_frame = {
            "ver": 11,
            "cmd": 1,
            "seq": 1,
            "opcode": OpCode.AUTH_SNAPSHOT,
            "payload": {
                "profile": {"id": 200000001},
                "contacts": [],
                "chats": [],
            },
        }
        await client._handle_frame(auth_success_frame)
        assert client.my_id == 200000001

    @pytest.mark.asyncio
    async def test_auth_success_current_web_profile(self):
        """appVersion 26.x nests the account under profile.contact."""
        client = MaxClient(token="tok", device_id="dev")
        await client._handle_frame({
            "ver": 11, "cmd": 1, "seq": 1, "opcode": OpCode.AUTH_SNAPSHOT,
            "payload": {
                "profile": {"contact": {"id": 200000001}, "profileOptions": []},
                "contacts": [], "chats": [],
            },
        })
        assert client.my_id == 200000001


class TestMessageHandling:
    def test_parse_incoming_message(self):
        client = MaxClient(token="tok", device_id="dev")
        payload = {
            "chatId": 12345,
            "message": {
                "sender": 67890,
                "text": "Hello",
                "id": "msg-1",
                "time": 1773136150000,
            },
        }
        msg = client._parse_message(payload)
        assert msg["chat_id"] == 12345
        assert msg["sender_id"] == 67890
        assert msg["text"] == "Hello"
        assert msg["message_id"] == "msg-1"

    def test_parse_message_no_text(self):
        client = MaxClient(token="tok", device_id="dev")
        payload = {"chatId": 1, "message": {"sender": 2, "attaches": []}}
        msg = client._parse_message(payload)
        assert msg["text"] == ""

    @pytest.mark.asyncio
    async def test_on_message_callback(self):
        received = []
        client = MaxClient(token="tok", device_id="dev")
        client.on_message = lambda msg: received.append(msg)

        payload = {
            "chatId": 100,
            "message": {"sender": 200, "text": "Test", "id": "m1", "time": 0, "attaches": []},
        }
        await client._handle_frame({
            "ver": 11, "cmd": 0, "seq": 1,
            "opcode": OpCode.MESSAGE, "payload": payload,
        })
        assert len(received) == 1
        assert received[0]["text"] == "Test"

    @pytest.mark.asyncio
    async def test_ignores_self_messages(self):
        received = []
        client = MaxClient(token="tok", device_id="dev")
        client.my_id = 200
        client.on_message = lambda msg: received.append(msg)

        payload = {
            "chatId": 100,
            "message": {"sender": 200, "text": "My own msg", "id": "m1", "time": 0, "attaches": []},
        }
        await client._handle_frame({
            "ver": 11, "cmd": 0, "seq": 1,
            "opcode": OpCode.MESSAGE, "payload": payload,
        })
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_dispatch_opcode_triggers_on_message(self):
        """DISPATCH opcode (128) should also trigger on_message."""
        received = []
        client = MaxClient(token="tok", device_id="dev")
        client.my_id = 100
        client.on_message = lambda msg: received.append(msg)

        payload = {
            "chatId": 200,
            "message": {"sender": 300, "text": "Dispatch test", "id": "m2", "time": 0, "attaches": []},
        }
        await client._handle_frame({
            "ver": 11, "cmd": 0, "seq": 2,
            "opcode": OpCode.DISPATCH, "payload": payload,
        })
        assert len(received) == 1
        assert received[0]["text"] == "Dispatch test"


class TestSendFailureHandling:
    @pytest.mark.asyncio
    async def test_send_returns_false_when_not_connected(self):
        client = MaxClient(token="t", device_id="d")
        assert await client.send_message(200, "Hi") is False

    @pytest.mark.asyncio
    async def test_send_returns_false_on_connection_error(self):
        """A drop mid-send must not escape into the Telegram handler."""
        class DeadWS:
            closed = False

            async def send_json(self, frame):
                raise ConnectionResetError("connection lost")

        client = MaxClient(token="t", device_id="d")
        client._ws = DeadWS()

        assert await client.send_message(200, "Hi") is False


class TestConnectionKeepalive:
    def test_ws_ping_interval_configured(self):
        """aiohttp needs heartbeat= to notice a half-open connection."""
        assert MaxClient.WS_PING_INTERVAL == 30

    @pytest.mark.asyncio
    async def test_connect_passes_heartbeat_to_ws_connect(self, monkeypatch):
        import aiohttp
        from unittest.mock import MagicMock, patch

        recorded = {}

        class FakeWS:
            closed = False

            async def send_json(self, frame):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def ws_connect(self, url, **kwargs):
                recorded.update(url=url, **kwargs)
                return FakeWS()

        client = MaxClient(token="t", device_id="d")

        async def stop_after_first_attempt(delay):
            client.stop()

        with patch.object(aiohttp, "ClientSession", lambda *a, **k: FakeSession()):
            monkeypatch.setattr("asyncio.sleep", stop_after_first_attempt)
            await client.connect()

        assert recorded["heartbeat"] == 30

    @pytest.mark.asyncio
    async def test_backoff_not_reset_without_auth(self, monkeypatch):
        """A rejected token must back off, not retry every 5s forever."""
        import aiohttp
        from unittest.mock import patch

        delays = []

        class FakeWS:
            closed = False

            async def send_json(self, frame):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def ws_connect(self, url, **kwargs):
                return FakeWS()

        client = MaxClient(token="bad", device_id="d")

        async def record_sleep(delay):
            delays.append(delay)
            if len(delays) >= 4:
                client.stop()

        with patch.object(aiohttp, "ClientSession", lambda *a, **k: FakeSession()):
            monkeypatch.setattr("asyncio.sleep", record_sleep)
            await client.connect()

        assert delays == [5, 10, 20, 40]

    @pytest.mark.asyncio
    async def test_backoff_resets_after_successful_auth(self, monkeypatch):
        import aiohttp
        from unittest.mock import patch

        delays = []

        class AuthWS:
            closed = False

            def __init__(self):
                self._frames = [{
                    "ver": 11, "cmd": 1, "seq": 1,
                    "opcode": int(OpCode.AUTH_SNAPSHOT),
                    "payload": {"profile": {"id": 7}, "contacts": [], "chats": []},
                }]

            async def send_json(self, frame):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._frames:
                    raise StopAsyncIteration
                frame = self._frames.pop(0)

                class Msg:
                    type = aiohttp.WSMsgType.TEXT

                    def __init__(self, data):
                        self._data = data

                    def json(self):
                        return self._data

                return Msg(frame)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def ws_connect(self, url, **kwargs):
                return AuthWS()

        client = MaxClient(token="good", device_id="d")

        async def record_sleep(delay):
            delays.append(delay)
            if len(delays) >= 3:
                client.stop()

        with patch.object(aiohttp, "ClientSession", lambda *a, **k: FakeSession()):
            monkeypatch.setattr("asyncio.sleep", record_sleep)
            await client.connect()

        assert client.my_id == 7
        assert delays == [5, 5, 5]
