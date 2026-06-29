# boringlog

**Structured logging for microservices — built on Python's standard library, not around it.**

Logging should be boring. No new runtime, no parallel logger object to learn, no
magic. `boringlog` is a thin layer over the standard library `logging` that gives
you sane defaults in one call, structured fields as keyword arguments, and shared
context on every line — then gets out of your way.

```python
from boringlog import configure_logging, get_logger

configure_logging(global_context={"source": "api", "worker_id": "w-1"})
log = get_logger(__name__)
log.info("server started", port=8000)
```

```
10:35:36 [INFO] [__main__]: server started | port: 8000 | source: api | worker_id: w-1
```

## Why "boring"?

Because boring is a feature. `boringlog` doesn't replace `logging` the way some
libraries do — it configures it. That means:

- **Nothing new to learn.** It's standard `logging` underneath. Your knowledge,
  your handlers, your `LOG_LEVEL` all still apply.
- **Third-party logs come along for free.** Because everything flows through the
  standard logging machinery, logs from `httpx`, `sqlalchemy`, `uvicorn`, etc. go
  through the same handlers and carry your global context too.
- **Zero dependencies.** It's a single, dependency-free module (~350 lines) you
  can read in one sitting and vendor into any project.

If you want a feature-rich, opinionated logging *framework*, reach for loguru or
structlog. If you want your standard library `logging` to just be configured well
and behave predictably across all your services, that's this.

## Requirements

Python 3.10+ (uses PEP 604 `X | Y` type unions). No third-party dependencies.

## Installation

`boringlog` is a single, dependency-free module.

```bash
pip install boringlog
```

In `requirements.txt`, pinned to a version (recommended):

```
boringlog==0.1.0
```

Or install from source — latest `main`, or pinned to a tag:

```bash
pip install git+https://github.com/kizeev/boringlog.git
pip install git+https://github.com/kizeev/boringlog.git@v0.1.0
```

Or just copy `src/boringlog/` into your project — it has no dependencies.

## Quick start

Call `configure_logging()` **once**, as early as possible in your process
entrypoint. Then grab a logger anywhere with `get_logger()`.

```python
from uuid import uuid4
from boringlog import configure_logging, get_logger

configure_logging(
    main_handlers=["console", "json_file"],
    logger_levels={"httpx": "CRITICAL"},
    global_context={"source": "extractor", "worker_id": str(uuid4())},
)

log = get_logger(__name__)
log.info("worker started", port=8000, mode="batch")
```

Console output:

```
10:35:36 [INFO] [__main__]: worker started | port: 8000 | mode: batch | source: extractor | worker_id: 3f2a...
```

JSON output (one object per line, written to `logs.jsonl` by default):

```json
{"timestamp": "2026-06-17T10:35:36.561665+00:00", "level": "INFO", "logger": "__main__", "message": "worker started", "port": 8000, "mode": "batch", "source": "extractor", "worker_id": "3f2a..."}
```

## Logging API

`get_logger()` returns a wrapper with the usual levels. Any keyword argument
becomes a structured field:

```python
log = get_logger(__name__)

log.debug("cache lookup", key="user:42", hit=False)
log.info("request handled", path="/items", status=200, ms=12.4)
log.warning("retrying", attempt=3, backoff_s=2)
log.error("upstream failed", url="https://...", status=503)
```

### Exceptions

Use `.exception()` inside an `except` block to attach a traceback, or pass
`exc_info=True` to `.error()` / `.critical()`:

```python
try:
    risky()
except ValueError:
    log.exception("failed to parse payload", payload_id=123)
```

## Context

There are three ways to attach context, and they answer different questions.

**Global context** — set once in `configure_logging(global_context=...)`. These
fields land on *every* log line in the process, from any logger, including
third-party libraries. This is where process-wide identifiers belong: service
name, worker id, deployment region, build version.

```python
configure_logging(global_context={"source": "api", "version": "1.4.2"})
```

**Per-logger context** — bound to a single logger via `get_logger(name, context=...)`.
These fields are merged into every call made through that wrapper. Use it for a
component or subsystem identifier.

```python
log = get_logger("api.billing", context={"component": "billing"})
log.info("charge created", amount=1999)  # carries component=billing
```

You can also derive a child wrapper with extra bound fields via `.bind()`:

```python
req_log = log.bind(request_id="abc-123")
req_log.info("validating")   # carries component=billing and request_id=abc-123
```

**Scope-local context** — set with `log_context(...)` (or `bind_context()`),
scoped to the current job, request, or task. Every log emitted while the block
is active carries the fields — from *any* logger, including nested functions
with their own loggers and third-party libraries — without threading a logger
through your call stack. It is safe under concurrency: each `asyncio` task (and
each thread) gets its own copy, so values never leak between jobs.

```python
from boringlog import log_context, get_logger

log = get_logger(__name__)

def process_job(job):
    with log_context(job_id=job.id):
        log.info("started")     # carries job_id
        handle(job)             # every log inside, from anywhere, carries job_id
    # outside the block, job_id is gone
```

For frameworks where a context manager doesn't fit (e.g. middleware), use the
token form directly:

```python
from boringlog import bind_context, reset_context

token = bind_context(request_id=req.id, user_id=req.user)
try:
    handle(req)
finally:
    reset_context(token)
```

**Generators caveat.** Generators share their caller's context, so a
`log_context(...)` block that spans a `yield` stays active in the consumer
between iterations — scope-local fields can "leak" onto the consumer's logs.
If that matters (e.g. a per-parser marker leaking onto a worker's logs),
decorate the generator function with `@isolate_generator`: it advances the
generator in its own context copy, so context set *inside* stays invisible to
the consumer, while context the consumer already had (e.g. `job_id`) remains
visible inside. The consumer keeps iterating normally — no special code.

```python
from boringlog import isolate_generator, log_context

@isolate_generator
def start_extracting(self):
    with log_context(parser_marker=self.parser.path):
        ...
        yield from self.extract(request)   # parser_marker won't leak to the consumer
```

When the whole generator should run under the same fields, `generator_context(**fields)`
collapses the decorator and the `with` into one line. Values may be static or
callables resolved against the call arguments (e.g. to read `self`):

```python
from boringlog import generator_context

@generator_context(parser_marker=lambda self: self.parser.path)
def start_extracting(self):
    ...                                    # no inner `with`; body un-indented
    yield from self.extract(request)
```

Precedence, from strongest to weakest: explicit per-call keyword arguments >
per-logger bound context (`.bind()` / `get_logger(context=...)`) > scope-local
context (`log_context`) > global context. A stronger field is never overwritten
by a weaker one.

## Configuration reference

`configure_logging(...)` accepts:

| Argument           | Type                | Default          | Description |
|--------------------|---------------------|------------------|-------------|
| `main_handlers`    | `list[str]`         | `["console"]`    | Built-in handlers to enable: `"console"` (stdout, human format) and/or `"json_file"`. |
| `extra_handlers`   | `dict[str, dict]`   | `{}`             | Additional `dictConfig` handler definitions to merge in. |
| `logger_levels`    | `dict[str, str]`    | `{}`             | Per-logger level overrides, e.g. `{"httpx": "CRITICAL"}`. |
| `json_log_path`    | `str`               | `"logs.jsonl"`   | Path for the JSON file handler (used when `json_file` is enabled). |
| `global_context`   | `dict[str, Any]`    | `None`           | Fields injected into every log record across the process. |
| `force`            | `bool`              | `False`          | Reconfigure even if logging was already configured. |

The root log level is read from the `LOG_LEVEL` environment variable
(default `INFO`). Handlers themselves accept `DEBUG`, so changing `LOG_LEVEL`
is enough to widen or narrow output.

```bash
LOG_LEVEL=DEBUG python -m myservice
```

### Custom handlers

`extra_handlers` takes standard `dictConfig` handler dicts. Reference the
built-in `"json"` or `"human"` formatters by name:

```python
configure_logging(
    main_handlers=["console"],
    extra_handlers={
        "errors_file": {
            "class": "logging.FileHandler",
            "filename": "errors.log",
            "level": "ERROR",
            "formatter": "json",
        },
    },
)
```

## Real-world example: a queue worker

A worker pulls jobs off a queue (RabbitMQ here), runs an extractor with one or
more parsers per job, and ships logs to the console, a JSON file, and Graylog —
all carrying the right identifiers without threading them through every call.

Configure once at startup. `source`/`worker_id` are global, third-party noise is
turned down, and a GELF handler is added with no extra code (the service depends
on `graypy`; boringlog stays dependency-free):

```python
from uuid import uuid4
from boringlog import configure_logging, get_logger, log_context

configure_logging(
    main_handlers=["console", "json_file"],
    extra_handlers={
        "graylog": {
            "level": "INFO",
            "class": "graypy.GELFTCPHandler",
            "host": "127.0.0.1",
            "port": 12201,
            "localname": SERVICE_NAME,
        },
    },
    logger_levels={"httpx": "CRITICAL", "pika": "CRITICAL"},
    global_context={"source": SERVICE_NAME, "worker_id": str(uuid4())},
)

logger = get_logger("worker")
```

Scope `job_id` to the whole job — every log during processing carries it,
including the extractor's and the parser's, and `_job_id` reaches Graylog as a
searchable field:

```python
def process_job(self, body):
    data = json.loads(body)
    job_id = data["job_id"]

    with log_context(job_id=job_id):
        logger.info("Processing job", parser=data["parser_path"])
        extractor = Extractor(job_id=job_id, parser_path=data["parser_path"])
        for msg in extractor.start_extracting():
            self.send_result_to_queue(job_id, msg)
        logger.info("Job done")
```

Inside the extractor, scope `parser_marker` to the parser run with one decorator.
It binds the marker for the whole generator and keeps it from leaking onto the
worker's own logs between yields:

```python
from boringlog import get_logger, generator_context

logger = get_logger("worker.extractor")

class Extractor:
    @generator_context(parser_marker=lambda self: self.parser.path)
    def start_extracting(self):
        logger.info("Run extractor")          # carries job_id + parser_marker
        for request in self.parser.start():
            yield from self.extract(request)   # parser_marker won't leak to the worker
        logger.info("Finish extractor")
```

The result, per layer: every line has `source` and `worker_id`; everything within
a job has `job_id`; everything emitted while a parser runs (extractor logs, the
parser's own logs, even a third-party library it calls) additionally has
`parser_marker`. None of it is passed by hand.

## Notes & behavior

- **Reserved field names are safe.** If you pass a field whose name collides
  with a `LogRecord` attribute (`module`, `name`, `message`, `msg`, `args`, …),
  it is automatically namespaced with a `ctx_` prefix instead of raising. So
  `log.info("x", module="parser")` logs `ctx_module: parser`.
- **Timestamps.** JSON output uses timezone-aware UTC ISO-8601
  (`...T10:35:36.561665+00:00`) for safe cross-service correlation. The console
  formatter shows local `HH:MM:SS` for readability.
- **Import order doesn't matter.** Existing loggers created before
  `configure_logging()` keep working — they are not disabled.
- **Non-JSON-serializable values** (`datetime`, `date`, `set`, `UUID`, …) are
  encoded gracefully, with a string fallback so a log call never fails to
  serialize.

## License

MIT. See [LICENSE](LICENSE).
