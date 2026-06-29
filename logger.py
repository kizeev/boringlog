"""
Structured logging utilities for standardizing logs across microservices.

Quick start:
    from .logger import configure_logging, get_logger

    # Call once, as early as possible, in the process entrypoint.
    configure_logging(
        main_handlers=['console', 'json_file'],
        logger_levels={'httpx': 'CRITICAL'},
        global_context={'source': SERVICE_NAME, 'worker_id': str(uuid4())},
    )

    logger = get_logger(__name__)
    logger.info('Started', port=8000)
"""
import contextvars
import datetime
import functools
import json
import logging
import logging.config
from contextlib import contextmanager
from os import getenv
from sys import stdout
from typing import Any
from uuid import UUID

__all__ = [
    'configure_logging',
    'get_logger',
    'StructuredLogger',
    'log_context',
    'bind_context',
    'reset_context',
    'clear_context',
    'isolate_generator',
    'generator_context',
    'JsonFormatter',
    'ReadableFormatter',
    'GlobalContextFilter',
    'ContextVarFilter',
]

_CONFIGURED = False

# Attributes that the stdlib puts on every LogRecord. Anything NOT in this set
# (and not starting with '_') is treated as a user-supplied structured field.
STANDARD_LOG_ATTRS = {
    'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename',
    'funcName', 'levelname', 'levelno', 'lineno', 'module', 'msecs', 'message',
    'msg', 'name', 'pathname', 'process', 'processName', 'relativeCreated',
    'stack_info', 'thread', 'threadName', 'taskName',
}

# Keys that cannot be placed in a record's __dict__ via `extra=` without the
# stdlib raising KeyError. Used to namespace colliding structured fields.
_RESERVED_KEYS = STANDARD_LOG_ATTRS | {'message', 'asctime'}

# Holds dynamic, scope-local context (per asyncio task / per thread). Set via
# bind_context()/log_context(); read by ContextVarFilter on every record.
# Default is None (never a shared mutable dict): callers always replace the
# value with a fresh dict, so the default can't be corrupted in place.
_context_var: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("boringlog_context", default=None))


def _sanitize_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """
    Rename any field whose key collides with a reserved LogRecord attribute,
    so that user data can never crash a log call (or clobber record internals).

    e.g. {'module': 'x'} -> {'ctx_module': 'x'}
    """
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        safe_key = f'ctx_{key}' if key in _RESERVED_KEYS else key
        safe[safe_key] = value
    return safe


def _utc_isoformat(created: float) -> str:
    """Timezone-aware UTC ISO-8601 timestamp from a record's `created` value."""
    return datetime.datetime.fromtimestamp(
        created, tz=datetime.timezone.utc).isoformat()


class GlobalContextFilter(logging.Filter):
    """
    Injects process-wide context into every log record that reaches a handler.

    Attach this to *handlers* (not to a single logger) so that the context is
    applied to all records regardless of which logger emitted them, including
    child loggers and third-party libraries.

    Args:
        context: key-value pairs injected into all records. Keys colliding with
            reserved LogRecord attributes are namespaced via `ctx_` prefix.
    """

    def __init__(self, context: dict[str, Any] | None = None):
        super().__init__()
        # Sanitize once; keys are now guaranteed safe to setattr.
        self.context = _sanitize_fields(context or {})

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in self.context.items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class ContextVarFilter(logging.Filter):
    """
    Injects dynamic, scope-local context (see bind_context / log_context) into
    every record reaching a handler. Attached to handlers, so it covers logs
    from any logger, including child loggers and third-party libraries.

    The stored context is already sanitized at bind time, so this filter only
    reads and assigns. Explicit per-call fields win: existing record attributes
    are not overwritten.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _context_var.get()
        if ctx:
            for key, value in ctx.items():
                if not hasattr(record, key):
                    setattr(record, key, value)
        return True


class CustomJsonEncoder(json.JSONEncoder):
    """JSON encoder that handles common non-serializable types."""

    def default(self, o: Any) -> Any:
        if isinstance(o, (datetime.date, datetime.datetime)):
            return o.isoformat()
        if isinstance(o, (set, frozenset, UUID)):
            return str(o)
        try:
            return super().default(o)
        except TypeError:
            return str(o)  # last-resort fallback, never crash a log call


class BaseFormatter(logging.Formatter):
    """Base class to extract user-supplied structured fields from a record."""

    def _get_extra_fields(self, record: logging.LogRecord) -> dict[str, Any]:
        """Return record attributes that are neither stdlib internals nor
        private (`_`-prefixed) bookkeeping."""
        return {
            k: v for k, v in record.__dict__.items()
            if k not in STANDARD_LOG_ATTRS and not k.startswith('_')
        }


class JsonFormatter(BaseFormatter):
    """Render logs as single-line JSON (one object per line)."""

    def format(self, record: logging.LogRecord) -> str:
        log_object: dict[str, Any] = {
            'timestamp': _utc_isoformat(record.created),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        log_object.update(self._get_extra_fields(record))

        if record.exc_info:
            log_object['exc_info'] = self.formatException(record.exc_info)

        return json.dumps(log_object, cls=CustomJsonEncoder)


class ReadableFormatter(BaseFormatter):
    """
    Human-friendly console output.

    Format: HH:MM:SS [LEVEL] [logger_name]: message | key: value | ...
    Timestamp is local time, intended for a developer watching a terminal.
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.datetime.fromtimestamp(
            record.created).strftime("%H:%M:%S")

        log_line = (
            f'{timestamp} [{record.levelname}] [{record.name}]: '
            f'{record.getMessage()}')

        extra_params = [
            f'{k}: {v}' for k, v in self._get_extra_fields(record).items()]
        if extra_params:
            log_line += ' | ' + ' | '.join(extra_params)

        if record.exc_info:
            error_text = self.formatException(record.exc_info)
            indented_error = '\n'.join(
                '\t' + line for line in error_text.splitlines())
            log_line += f'\n{indented_error}'

        return log_line


class StructuredLogger:
    """
    A structured logging interface over a stdlib logging.Logger.

    Extra fields are passed as keyword arguments. Optional per-logger `context`
    is bound to the logger and merged into every call (explicit call kwargs win
    on key conflicts). Field keys that collide with reserved LogRecord
    attributes are namespaced automatically, so a log call can never raise.

    Example:
        >>> logger = get_logger(__name__, context={'component': 'parser'})
        >>> logger.info('User action', user_id=123, action='login')
    """

    def __init__(
        self,
        logger: logging.Logger,
        context: dict[str, Any] | None = None,
    ):
        self._logger = logger
        self._context = context or {}

    def _log(
        self, level: int, msg: str, /, exc_info: bool = False, **kwargs: Any
    ) -> None:
        if not self._logger.isEnabledFor(level):
            return
        merged = {**self._context, **kwargs}
        self._logger.log(
            level, msg, exc_info=exc_info, extra=_sanitize_fields(merged))

    # `msg` is positional-only (`/`) so a structured field literally named
    # "msg" lands in **kwargs instead of colliding with the parameter.
    def debug(self, msg: str, /, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, **kwargs)

    def info(self, msg: str, /, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, **kwargs)

    def warning(self, msg: str, /, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, /, exc_info: bool = False, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, exc_info=exc_info, **kwargs)

    def critical(
        self, msg: str, /, exc_info: bool = False, **kwargs: Any
    ) -> None:
        self._log(logging.CRITICAL, msg, exc_info=exc_info, **kwargs)

    def exception(self, msg: str, /, **kwargs: Any) -> None:
        """Log an error with traceback. Call from within an except block."""
        self._log(logging.ERROR, msg, exc_info=True, **kwargs)

    def bind(self, **context: Any) -> 'StructuredLogger':
        """Return a new logger with additional bound context."""
        return StructuredLogger(self._logger, {**self._context, **context})


def configure_logging(
    main_handlers: list[str] | None = None,
    extra_handlers: dict[str, dict[str, Any]] | None = None,
    logger_levels: dict[str, str] | None = None,
    json_log_path: str = 'logs.jsonl',
    global_context: dict[str, Any] | None = None,
    force: bool = False,
) -> None:
    """
    Configure logging with the given handlers and formatters.

    Args:
        main_handlers: built-in handlers to enable ('console' and/or
            'json_file'). Defaults to ['console'].
        extra_handlers: additional dictConfig handler definitions to merge in.
        logger_levels: per-logger level overrides, e.g. {'httpx': 'CRITICAL'}.
            The key is a logger name (a third-party library or your own module);
            the value is its minimum level.
        json_log_path: path for the JSON file handler (if 'json_file' enabled).
        global_context: process-wide fields (e.g. service name, worker id)
            injected into EVERY log line via a handler-level filter. This is the
            right place for fields that should appear on all logs, including
            those from third-party libraries.
        force: reconfigure even if already configured.

    Example:
        >>> configure_logging(
        ...     main_handlers=['console', 'json_file'],
        ...     logger_levels={'requests': 'WARNING'},
        ...     global_context={'source': 'api', 'worker_id': 'w-1'},
        ... )
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        logging.getLogger(__name__).debug(
            "Logging already configured. Use force=True to reconfigure.")
        return

    if main_handlers is None:
        main_handlers = ['console']
    if extra_handlers is None:
        extra_handlers = {}
    if logger_levels is None:
        logger_levels = {}

    level = getenv('LOG_LEVEL', 'INFO').upper()

    main_handlers_dict = {
        'console': {
            'class': 'logging.StreamHandler',
            'level': 'DEBUG',
            'formatter': 'human',
            'stream': stdout,
        },
        'json_file': {
            'class': 'logging.FileHandler',
            'filename': json_log_path,
            'level': 'DEBUG',
            'formatter': 'json',
            'encoding': 'utf-8',
        },
    }

    unknown = [h for h in main_handlers if h not in main_handlers_dict]
    if unknown:
        raise ValueError(
            f"Unknown handler(s) in main_handlers: {unknown}. "
            f"Built-in handlers are {sorted(main_handlers_dict)}; "
            f"define any others via extra_handlers.")

    handlers = {name: main_handlers_dict[name] for name in main_handlers}
    handlers.update(extra_handlers)
    handlers_list = list(handlers.keys())

    config = {
        'version': 1,
        # False: do not silently kill loggers created before this call (e.g.
        # module-level loggers in already-imported modules).
        'disable_existing_loggers': False,
        'formatters': {
            'json': {'()': JsonFormatter},
            'human': {'()': ReadableFormatter},
        },
        'handlers': {**handlers},
        'loggers': {
            '': {
                'handlers': handlers_list,
                'level': level,
            },
        },
    }

    for logger_name, logger_level in logger_levels.items():
        config['loggers'][logger_name] = {
            'handlers': handlers_list,
            'level': logger_level,
            'propagate': False,
        }

    logging.config.dictConfig(config)

    # Attach context at the HANDLER level so it lands on every record reaching
    # the handler, regardless of originating logger. Handler instances are
    # shared across all configured loggers, so root's handlers cover them.
    root_handlers = logging.getLogger().handlers
    # Dynamic, scope-local context is ALWAYS available (bind_context/log_context).
    for handler in root_handlers:
        handler.addFilter(ContextVarFilter())
    # Static process-wide context only if provided.
    if global_context:
        ctx_filter = GlobalContextFilter(global_context)
        for handler in root_handlers:
            handler.addFilter(ctx_filter)

    _CONFIGURED = True


def bind_context(**fields: Any) -> contextvars.Token:
    """
    Add fields to the current scope's logging context. Every subsequent log in
    this scope (asyncio task / thread) carries them, from any logger.

    Returns a token; pass it to reset_context() to undo. Prefer log_context()
    as a context manager when you can.
    """
    current = _context_var.get() or {}
    return _context_var.set({**current, **_sanitize_fields(fields)})


def reset_context(token: contextvars.Token) -> None:
    """Restore the logging context to the state captured by bind_context()."""
    _context_var.reset(token)


def clear_context() -> contextvars.Token:
    """Drop all scope-local context. Returns a token to restore it."""
    return _context_var.set({})


@contextmanager
def log_context(**fields: Any):
    """
    Scope job/request-level fields onto all logs within the block.

        with log_context(job_id=job.id):
            handle(job)   # every log here carries job_id
    """
    token = bind_context(**fields)
    try:
        yield
    finally:
        reset_context(token)


def isolate_generator(genfunc):
    """
    Decorator for a generator function whose body sets scope-local context
    (via log_context / bind_context) that must NOT leak to the code consuming
    the generator.

    Generators share the caller's context, so a `log_context(...)` block that
    spans a `yield` would otherwise stay active in the consumer between
    iterations. This decorator advances the generator inside its own copied
    context, so context set inside is invisible to the consumer, while context
    the consumer already had (e.g. job_id) remains visible inside.

        @isolate_generator
        def start_extracting(self):
            with log_context(parser_marker=self.parser.path):
                ...
                yield from self.extract(request)

    The consumer iterates normally (`for msg in start_extracting(): ...`) — no
    special driving code required. Supports plain iteration and early exit;
    if you rely on .send()/.throw() into the generator, drive it manually
    instead.
    """
    @functools.wraps(genfunc)
    def wrapper(*args: Any, **kwargs: Any):
        gen = genfunc(*args, **kwargs)
        # Snapshot taken on first iteration -> inherits the consumer's context
        # (e.g. an outer log_context) while isolating changes made inside.
        ctx = contextvars.copy_context()
        try:
            while True:
                try:
                    item = ctx.run(next, gen)
                except StopIteration:
                    return
                yield item
        finally:
            ctx.run(gen.close)
    return wrapper


def generator_context(**fields: Any):
    """
    Decorator that runs a generator function isolated AND with the given
    scope-local fields bound for its entire run. It is `isolate_generator` +
    `log_context` in one, so you don't write a `with log_context(...)` inside.

    Field values may be static, or callables that receive the same arguments as
    the generator call — use a callable to read a value off `self` or an arg:

        @generator_context(parser_marker=lambda self: self.parser.path)
        def start_extracting(self):
            ...                      # every log here carries parser_marker
            yield from self.extract(request)

        @generator_context(component="parser")     # static value
        def gen():
            ...

    The bound fields reach every log emitted while the generator runs (nested
    functions, third-party libs) but do NOT leak to the code consuming it.
    Context the consumer already had (e.g. job_id) stays visible inside.

    Note: a field value that is itself callable will be *called* to resolve it.
    To log a callable as a value, set it inside the body via log_context().
    """
    def decorate(genfunc):
        @functools.wraps(genfunc)
        def wrapper(*args: Any, **kwargs: Any):
            gen = genfunc(*args, **kwargs)
            ctx = contextvars.copy_context()
            resolved = {
                key: (value(*args, **kwargs) if callable(value) else value)
                for key, value in fields.items()
            }
            ctx.run(bind_context, **resolved)
            try:
                while True:
                    try:
                        item = ctx.run(next, gen)
                    except StopIteration:
                        return
                    yield item
            finally:
                ctx.run(gen.close)
        return wrapper
    return decorate


def get_logger(
    name: str,
    context: dict[str, Any] | None = None,
) -> StructuredLogger:
    """
    Get a structured logger.

    Args:
        name: logger name (typically __name__).
        context: optional fields bound to THIS logger's calls (merged into
            every call from the returned logger). For process-wide fields that
            must appear on all logs, use `configure_logging(global_context=...)`
            instead.

    Returns:
        StructuredLogger instance.

    Example:
        >>> logger = get_logger(__name__, context={'component': 'parser'})
        >>> logger.info('Started', port=8000)
    """
    return StructuredLogger(logging.getLogger(name), context)
