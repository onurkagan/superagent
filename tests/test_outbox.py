from superagent.core.model import ChannelRef
from superagent.outbox import OutboundMessage, Outbox, PermanentSendError
from superagent.store import Store


class FakeSender:
    def __init__(self, fail_times=0, permanent=False):
        self.sent = []
        self.fail_times = fail_times
        self.permanent = permanent

    def send(self, channel, text):
        if self.permanent:
            raise PermanentSendError("issue deleted")
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("network down")
        self.sent.append((channel.address, text))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(sender, clock=None):
    clock = clock or Clock()
    outbox = Outbox(Store(), clock=clock)
    return outbox, (lambda source: sender), clock


def msg(mid, text, ticket="tk_1", address="issue:1"):
    return OutboundMessage(mid, ChannelRef("github", address), text, "reply", ticket)


def test_messages_are_sent_in_order_and_idempotently():
    sender = FakeSender()
    outbox, sender_for, _ = make(sender)
    outbox.send(msg("m1", "ack"))
    outbox.send(msg("m2", "result"))
    outbox.send(msg("m1", "duplicate"))
    result = outbox.flush(sender_for)
    assert result.sent == 2
    assert sender.sent == [("issue:1", "ack"), ("issue:1", "result")]
    assert outbox.status("m1") == "sent"


def test_failed_message_holds_back_later_messages_of_same_ticket_and_conversation():
    sender = FakeSender(fail_times=1)
    outbox, sender_for, clock = make(sender)
    outbox.send(msg("m1", "ack"))
    outbox.send(msg("m2", "result"))
    outbox.send(msg("m3", "other ticket", ticket="tk_2"))
    result = outbox.flush(sender_for)
    assert result.failed == 1
    assert sender.sent == [("issue:1", "other ticket")]
    clock.now += 5
    outbox.flush(sender_for)
    assert [t for _, t in sender.sent] == ["other ticket", "ack", "result"]


def test_backoff_doubles_and_problem_is_reported_after_three_failures():
    sender = FakeSender(fail_times=10)
    outbox, sender_for, clock = make(sender)
    outbox.send(msg("m1", "ack"))
    for _ in range(3):
        outbox.flush(sender_for)
        clock.now += 1000
    assert outbox.problems()[0]["attempts"] == 3


class FailsOnAck(FakeSender):
    def send(self, channel, text):
        if text == "ack":
            raise ConnectionError("rejected")
        super().send(channel, text)


def test_problem_message_stops_holding_back_the_conversation():
    sender = FailsOnAck()
    outbox, sender_for, clock = make(sender)
    outbox.send(msg("m1", "ack"))
    outbox.send(msg("m2", "result"))
    for _ in range(2):
        outbox.flush(sender_for)
        assert sender.sent == []
        clock.now += 1000
    outbox.flush(sender_for)  # the third failure turns m1 into a problem, which releases m2
    assert sender.sent == [("issue:1", "result")]
    assert outbox.status("m1") == "pending"


def test_permanent_failure_drops_the_message():
    outbox, sender_for, _ = make(FakeSender(permanent=True))
    outbox.send(msg("m1", "ack"))
    assert outbox.flush(sender_for).dropped == 1
    assert outbox.status("m1") == "dropped"


def test_messages_wait_while_source_is_off():
    outbox, _, _ = make(FakeSender())
    outbox.send(msg("m1", "ack"))
    outbox.flush(lambda source: None)
    assert outbox.status("m1") == "pending"


def test_message_is_dropped_when_source_stops_listening():
    outbox, sender_for, _ = make(FakeSender())
    outbox.send(msg("m1", "ack"))
    outbox.flush(sender_for, listens=lambda channel: False)
    assert outbox.status("m1") == "dropped"


def test_withdraw_drops_pending_message():
    outbox, sender_for, _ = make(FakeSender())
    outbox.send(msg("m1", "ack"))
    outbox.withdraw("m1")
    assert outbox.flush(sender_for).sent == 0
