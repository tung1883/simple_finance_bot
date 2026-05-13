from chat_history import clear_chat_history, get_chat_history, save_chat_message


class TestChatHistory:
    def test_save_and_get(self):
        save_chat_message(1, "user", "hello")
        save_chat_message(1, "assistant", "hi")
        rows = get_chat_history(1, limit=10)
        assert rows == [("user", "hello"), ("assistant", "hi")]

    def test_clear(self):
        save_chat_message(1, "user", "hello")
        clear_chat_history(1)
        assert get_chat_history(1) == []

    def test_per_user_isolation(self):
        save_chat_message(1, "user", "for 1")
        save_chat_message(2, "user", "for 2")
        assert get_chat_history(1) == [("user", "for 1")]
        assert get_chat_history(2) == [("user", "for 2")]

    def test_limit_returns_newest(self):
        for i in range(20):
            save_chat_message(1, "user", f"msg-{i}")
        rows = get_chat_history(1, limit=3)
        assert len(rows) == 3
        # get_chat_history reverses DESC fetch → returned in chronological order with newest last
        assert rows[-1] == ("user", "msg-19")
