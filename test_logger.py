"""
Tests for boringlog.

The value of this library is correctness — context reaching every log, not
leaking across generator boundaries, predictable precedence, and safe handling
of awkward field values. These tests pin exactly that.

Each test configures logging to a temp JSON file and reads the records back,
which exercises the JSON formatter, the handler-level context filters, and the
public API together.
"""
import asyncio
import datetime
import json
import logging
import uuid
from pathlib import Path

import pytest

from boringlog import (
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
    isolate_generator,
    log_context,
    reset_context,
    generator_context,
)


@pytest.fixture
def logfile(tmp_path):
    return tmp_path / "logs.jsonl"


@pytest.fixture(autouse=True)
def _clean_context():
    """Keep scope-local context from leaking between tests."""
    clear_context()
    yield
    clear_context()


def setup_logs(logfile, **kwargs):
    configure_logging(
        main_handlers=["json_file"],
        json_log_path=str(logfile),
        force=True,
        **kwargs,
    )


def read_records(logfile):
    text = Path(logfile).read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in text.splitlines() if line]


# --------------------------------------------------------------------------- #
# Global context
# --------------------------------------------------------------------------- #
def test_global_context_reaches_every_logger(logfile):
    setup_logs(logfile, global_context={"source": "svc", "worker_id": "w-1"})

    get_logger("my.app").info("from app")
    logging.getLogger("some.third.party").warning("from a lib")

    recs = read_records(logfile)
    assert len(recs) == 2
    for rec in recs:
        assert rec["source"] == "svc"
        assert rec["worker_id"] == "w-1"


def test_no_global_context_still_works(logfile):
    setup_logs(logfile)
    get_logger("x").info("hello")
    recs = read_records(logfile)
    assert recs[0]["message"] == "hello"
    assert "source" not in recs[0]


# --------------------------------------------------------------------------- #
# Structured fields & per-logger bound context
# --------------------------------------------------------------------------- #
def test_keyword_args_become_fields(logfile):
    setup_logs(logfile)
    get_logger("x").info("event", user_id=123, action="login")
    rec = read_records(logfile)[0]
    assert rec["user_id"] == 123
    assert rec["action"] == "login"


def test_per_logger_bound_context(logfile):
    setup_logs(logfile)
    log = get_logger("billing", context={"component": "billing"})
    log.info("charge", amount=1999)
    rec = read_records(logfile)[0]
    assert rec["component"] == "billing"
    assert rec["amount"] == 1999


def test_bind_returns_child_with_extra_context(logfile):
    setup_logs(logfile)
    log = get_logger("x", context={"component": "billing"})
    req_log = log.bind(request_id="abc-123")
    req_log.info("validating")
    rec = read_records(logfile)[0]
    assert rec["component"] == "billing"
    assert rec["request_id"] == "abc-123"


# --------------------------------------------------------------------------- #
# Scope-local context (log_context / bind_context)
# --------------------------------------------------------------------------- #
def test_log_context_applies_then_resets(logfile):
    setup_logs(logfile)
    log = get_logger("x")
    with log_context(job_id="job-1"):
        log.info("inside")
    log.info("outside")
    recs = read_records(logfile)
    assert recs[0]["job_id"] == "job-1"
    assert "job_id" not in recs[1]


def test_log_context_reaches_other_loggers(logfile):
    setup_logs(logfile)
    with log_context(job_id="job-1"):
        get_logger("a.b").info("one")
        logging.getLogger("third.party").info("two")
    recs = read_records(logfile)
    assert all(rec["job_id"] == "job-1" for rec in recs)


def test_bind_reset_context_token(logfile):
    setup_logs(logfile)
    log = get_logger("x")
    token = bind_context(request_id="r-1")
    try:
        log.info("inside")
    finally:
        reset_context(token)
    log.info("outside")
    recs = read_records(logfile)
    assert recs[0]["request_id"] == "r-1"
    assert "request_id" not in recs[1]


# --------------------------------------------------------------------------- #
# Reserved-key safety
# --------------------------------------------------------------------------- #
def test_reserved_field_names_are_namespaced(logfile):
    setup_logs(logfile)
    log = get_logger("x")
    # None of these may raise; all collide with LogRecord attributes.
    log.info("event", module="m", name="n", message="msg", msg="w", args="a")
    rec = read_records(logfile)[0]
    assert rec["ctx_module"] == "m"
    assert rec["ctx_name"] == "n"
    assert rec["ctx_message"] == "msg"
    assert rec["ctx_msg"] == "w"
    assert rec["ctx_args"] == "a"
    # The real message is intact.
    assert rec["message"] == "event"


# --------------------------------------------------------------------------- #
# JSON serialization
# --------------------------------------------------------------------------- #
def test_non_serializable_values_are_encoded(logfile):
    setup_logs(logfile)
    uid = uuid.uuid4()
    when = datetime.datetime(2020, 1, 2, 3, 4, 5)
    get_logger("x").info("event", ids={1, 2, 3}, uid=uid, when=when)
    rec = read_records(logfile)[0]
    assert rec["uid"] == str(uid)
    assert rec["when"].startswith("2020-01-02T03:04:05")
    assert isinstance(rec["ids"], str)  # set -> str repr, no crash


def test_json_timestamp_is_utc_aware(logfile):
    setup_logs(logfile)
    get_logger("x").info("event")
    rec = read_records(logfile)[0]
    ts = datetime.datetime.fromisoformat(rec["timestamp"])
    assert ts.utcoffset() == datetime.timedelta(0)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
def test_exception_attaches_traceback(logfile):
    setup_logs(logfile)
    log = get_logger("x")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("failed", payload_id=7)
    rec = read_records(logfile)[0]
    assert rec["payload_id"] == 7
    assert "ValueError" in rec["exc_info"]
    assert "Traceback" in rec["exc_info"]


# --------------------------------------------------------------------------- #
# Precedence: call > bound > scope-local > global
# --------------------------------------------------------------------------- #
def test_field_precedence(logfile):
    setup_logs(logfile, global_context={"k": "global"})
    bound = get_logger("p", context={"k": "bound"})
    plain = get_logger("q")

    with log_context(k="scope"):
        bound.info("all", k="call")   # call wins
        bound.info("bound_wins")      # bound > scope > global
        plain.info("scope_wins")      # scope > global
    plain.info("global_wins")         # only global

    recs = read_records(logfile)
    assert recs[0]["k"] == "call"
    assert recs[1]["k"] == "bound"
    assert recs[2]["k"] == "scope"
    assert recs[3]["k"] == "global"


# --------------------------------------------------------------------------- #
# Generators: isolation
# --------------------------------------------------------------------------- #
def test_isolate_generator_does_not_leak_to_consumer(logfile):
    setup_logs(logfile)
    gen_log = get_logger("gen")
    cons_log = get_logger("cons")

    @isolate_generator
    def produce():
        with log_context(marker="M"):
            gen_log.info("inside-1")
            yield 1
            gen_log.info("inside-2")
            yield 2

    with log_context(job_id="J"):
        for x in produce():
            cons_log.info("consume", x=x)

    recs = read_records(logfile)
    inside = [r for r in recs if r["logger"] == "gen"]
    consume = [r for r in recs if r["logger"] == "cons"]

    # Generator's own logs carry both the outer job_id and the inner marker.
    assert all(r["marker"] == "M" and r["job_id"] == "J" for r in inside)
    # Consumer logs carry job_id but the marker never leaked across the yield.
    assert all(r["job_id"] == "J" and "marker" not in r for r in consume)


def test_isolate_generator_cleans_up_on_early_break(logfile):
    setup_logs(logfile)
    cons_log = get_logger("cons")

    @isolate_generator
    def produce():
        with log_context(marker="M"):
            yield 1
            yield 2
            yield 3

    with log_context(job_id="J"):
        for x in produce():
            cons_log.info("got", x=x)
            break
        cons_log.info("after-break")

    recs = read_records(logfile)
    # No record should carry the leaked marker, including after the break.
    assert all("marker" not in r for r in recs)
    assert any(r["message"] == "after-break" for r in recs)


def test_generator_context_binds_and_isolates_with_callable(logfile):
    setup_logs(logfile)
    gen_log = get_logger("gen")
    cons_log = get_logger("cons")

    class Parser:
        path = "shop@v1"

    class Extractor:
        parser = Parser()

        @generator_context(parser_marker=lambda self: self.parser.path)
        def start(self):
            gen_log.info("run")
            yield "item-1"
            gen_log.info("done")

    with log_context(job_id="J"):
        for item in Extractor().start():
            cons_log.info("send", item=item)

    recs = read_records(logfile)
    gen = [r for r in recs if r["logger"] == "gen"]
    cons = [r for r in recs if r["logger"] == "cons"]
    assert all(r["parser_marker"] == "shop@v1" and r["job_id"] == "J" for r in gen)
    assert all(r["job_id"] == "J" and "parser_marker" not in r for r in cons)


def test_generator_context_static_value(logfile):
    setup_logs(logfile)
    log = get_logger("gen")

    @generator_context(component="parser")
    def produce():
        log.info("x")
        yield 1

    list(produce())
    rec = read_records(logfile)[0]
    assert rec["component"] == "parser"


# --------------------------------------------------------------------------- #
# Concurrency isolation
# --------------------------------------------------------------------------- #
def test_asyncio_tasks_do_not_share_context(logfile):
    setup_logs(logfile)
    log = get_logger("worker")

    async def task(job_id, steps):
        with log_context(job_id=job_id):
            for i in range(steps):
                log.info("step", i=i)
                await asyncio.sleep(0.001)

    async def main():
        await asyncio.gather(task("A", 3), task("B", 3))

    asyncio.run(main())

    recs = [r for r in read_records(logfile) if r["message"] == "step"]
    a = [r for r in recs if r["job_id"] == "A"]
    b = [r for r in recs if r["job_id"] == "B"]
    assert len(a) == 3 and len(b) == 3
    # Every step carries a job_id, and only "A" or "B" ever appears.
    assert all(r["job_id"] in {"A", "B"} for r in recs)


# --------------------------------------------------------------------------- #
# dictConfig behavior
# --------------------------------------------------------------------------- #
def test_existing_loggers_are_not_disabled(logfile):
    # Logger created BEFORE configure_logging must keep working.
    pre = logging.getLogger("created.before.configure")
    setup_logs(logfile)
    assert pre.disabled is False
    pre.info("survived")
    recs = read_records(logfile)
    assert any(r["message"] == "survived" for r in recs)


def test_unknown_main_handler_raises_clear_error(logfile):
    with pytest.raises(ValueError, match="Unknown handler"):
        configure_logging(main_handlers=["consoel"], force=True)


def test_context_is_isolated_between_contexts(logfile):
    # Hardening: the contextvar default is not a shared mutable dict, so
    # binding in one context never affects a sibling context.
    import contextvars

    from boringlog.logger import _context_var
    setup_logs(logfile)

    ctx_a = contextvars.copy_context()
    ctx_b = contextvars.copy_context()
    ctx_a.run(bind_context, a=1)
    assert ctx_a.run(_context_var.get) == {"a": 1}
    assert ctx_b.run(_context_var.get) in (None, {})
