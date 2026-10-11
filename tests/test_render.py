from tg_bridge.render import in_line, message_text, placeholder


def test_placeholders():
    assert placeholder({"media": "photo"}) == "[photo]"
    assert placeholder({"media": "voice", "duration": 72}) == "[voice 1:12]"
    assert placeholder({"media": "voice"}) == "[voice]"
    assert placeholder({"media": "video_note", "duration": 9}) == "[video message 0:09]"
    assert placeholder({"media": "document: report.pdf"}) == "[file report.pdf]"
    assert placeholder({"media": "sticker 😀"}) == "[sticker 😀]"
    assert placeholder({"media": "poll", "poll": {"question": "Lunch?"}}) == "[poll: Lunch?]"
    assert placeholder({"media": "geo"}) == "[location]"
    assert placeholder({"media": "something new"}) == "[media]"
    assert placeholder({}) == ""


def test_text_with_forward_and_transcript():
    assert (
        message_text({"text": "hey", "forwarded": {"from_user": "Bob"}})
        == "[forwarded from Bob] hey"
    )
    assert (
        message_text({"media": "voice", "duration": 5, "transcript": "hello"})
        == "[voice 0:05] (transcript, not verbatim) hello"
    )


def test_in_line_defaults_addressed_from_chat_type():
    rec = {"id": 5, "sender": "Ada", "sender_id": 1, "text": "x"}
    assert in_line(rec, "private")["addressed"] is True
    assert in_line(rec, "group")["addressed"] is False
    assert in_line(dict(rec, out=True), "private")["addressed"] is False


def test_in_line_without_sender():
    line = in_line({"id": 5, "text": "x"}, "group")
    assert line["author"] == {"id": "", "name": "Unknown", "self": False}
    assert "reply_to" not in line and "at" not in line
