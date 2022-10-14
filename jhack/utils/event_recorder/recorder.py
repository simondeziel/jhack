#!/usr/bin/env python3
import base64
import datetime
import functools
import inspect
import json
import os
import pickle
import typing
import warnings
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Literal, Optional, Tuple, Union

try:
    from jhack.logger import logger as jhack_logger
except ModuleNotFoundError:
    # running in unit
    from logging import getLogger

    jhack_logger = getLogger()

DEFAULT_DB_NAME = "event_db.json"
MEMO_REPLAY_INDEX_KEY = "MEMO_REPLAY_IDX"
MEMO_DATABASE_NAME_KEY = "MEMO_DATABASE_NAME"
MEMO_MODE_KEY = "MEMO_MODE"
DEFAULT_NAMESPACE = "<DEFAULT>"
LOGFILE = "jhack-replay-logs.txt"
SUPPORTED_SERIALIZERS = Literal["pickle", "json"]

logger = jhack_logger.getChild("recorder")

MemoModes = Literal["record", "replay"]
_CachingPolicy = Literal["strict", "loose"]

# notify just once of what mode we're running in
_PRINTED_MODE = False

# flag to mark when a memo cache's return value is not found as opposed to being None
_NotFound = object()


def _check_caching_policy(policy: _CachingPolicy) -> _CachingPolicy:
    if policy in {"strict", "loose"}:
        return policy
    else:
        logger.warning(
            f"invalid caching policy: {policy!r}. " f"defaulting to `strict`"
        )
    return "strict"


def _load_memo_mode() -> MemoModes:
    global _PRINTED_MODE

    val = os.getenv(MEMO_MODE_KEY, "record")
    if val == "record":
        # don't use logger, but print, to avoid recursion issues with juju-log.
        if not _PRINTED_MODE:
            print("MEMO: recording")
    elif val == "replay":
        if not _PRINTED_MODE:
            print("MEMO: replaying")
    else:
        warnings.warn(
            f"[ERROR]: MEMO: invalid value ({val!r}). Defaulting to `record`."
        )
        _PRINTED_MODE = True
        return "record"

    _PRINTED_MODE = True
    return typing.cast(MemoModes, val)


def _is_serializable(obj: Any, dumper: Callable[[Any], str]):
    try:
        dumper(obj)
        return True
    except Exception:  # noqa
        return False


def _log_memo(
    fn: Callable,
    args,
    kwargs,
    recorded_output: Any = None,
    cache_hit: bool = False,
    # use print, not logger calls, else the root logger will recurse if
    # juju-log calls are being @memo'd.
    log_fn: Callable[[str], None] = print,
):
    try:
        output_repr = repr(recorded_output)
    except:  # noqa catchall
        output_repr = "<repr failed: cannot repr(memoized output).>"

    trim = output_repr[:100]
    trimmed = "[...]" if len(output_repr) > 100 else ""
    hit = "hit" if cache_hit else "miss"

    fn_name = getattr(fn, "__name__", str(fn))

    if _self := getattr(fn, "__self__", None):
        # it's a method
        fn_repr = type(_self).__name__ + fn_name
    else:
        fn_repr = fn_name

    log_fn(
        f"@memo[{hit}]: replaying {fn_repr}(*{args}, **{kwargs})"
        f"\n\t --> {trim!r}{trimmed}"
    )


def memo(
    namespace: str = DEFAULT_NAMESPACE,
    name: str = None,
    caching_policy: _CachingPolicy = "strict",
    log_on_replay: bool = True,
    serializer: Optional[SUPPORTED_SERIALIZERS] = "json",
):
    def decorator(fn):
        if not inspect.isfunction(fn):
            raise RuntimeError(f"Cannot memoize non-function obj {fn!r}.")

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):

            _MEMO_MODE: MemoModes = _load_memo_mode()

            def _load(obj: str):
                if serializer == "pickle":
                    byt = base64.b64decode(obj)
                    return pickle.loads(byt)
                elif serializer == "json":
                    return json.loads(obj)
                raise ValueError(f"Invalid serializer: {serializer!r}")

            def _dump(obj: Any):
                if serializer == "pickle":
                    byt = pickle.dumps(obj)
                    return base64.b64encode(byt).decode('utf-8')
                elif serializer == "json":
                    return json.dumps(obj)
                raise ValueError(f"Invalid serializer: {serializer!r}")

            def propagate():
                """Make the real wrapped call."""

                if _MEMO_MODE == "replay" and log_on_replay:
                    _log_memo(fn, args, kwargs, "n/a", cache_hit=False)

                # todo: if we are replaying, should we be caching this result?
                return fn(*args, **kwargs)

            memoizable_args = args
            if args:
                if not _is_serializable(args[0], dumper=_dump):
                    # probably we're wrapping a method! which means args[0] is `self`
                    # we can't use `inspect.ismethod(fn)` because at @memo-execution-time,
                    # `fn` isn't a method yet!
                    memoizable_args = args[1:]
                else:
                    memoizable_args = args

            # convert args to list for comparison purposes because memos are
            # loaded from json, where tuples become lists.
            memo_args = list(memoizable_args)

            database = os.environ.get(MEMO_DATABASE_NAME_KEY, DEFAULT_DB_NAME)
            with event_db(database) as data:
                if not data.scenes:
                    raise RuntimeError("No scenes: cannot memoize.")
                idx = os.environ.get(MEMO_REPLAY_INDEX_KEY, None)

                strict_caching = _check_caching_policy(caching_policy) == "strict"

                memo_name = f"{namespace}.{name or fn.__name__}"

                if _MEMO_MODE == "record":
                    memo = data.scenes[-1].context.memos.get(memo_name)
                    if memo is None:
                        cpolicy_name = typing.cast(
                            _CachingPolicy, "strict" if strict_caching else "loose"
                        )
                        memo = Memo(caching_policy=cpolicy_name, serializer='json')

                    output = propagate()

                    # we can't hash dicts, so we dump args and kwargs
                    # regardless of what they are
                    serialized_args_kwargs = _dump((memo_args, kwargs))
                    serialized_output = _dump(output)

                    memo.cache_call(serialized_args_kwargs, serialized_output)
                    data.scenes[-1].context.memos[memo_name] = memo
                    return output

                elif _MEMO_MODE == "replay":
                    if idx is None:
                        raise RuntimeError(
                            f"provide a {MEMO_REPLAY_INDEX_KEY} envvar"
                            "to tell the replay environ which scene to look at"
                        )
                    try:
                        idx = int(idx)
                    except TypeError:
                        raise RuntimeError(
                            f"invalid idx: ({idx}); expecting an integer."
                        )

                    try:
                        memo = data.scenes[idx].context.memos[memo_name]

                    except KeyError:
                        # if no memo is present for this function, that might mean that
                        # in the recorded session it was not called (this path is new!)
                        warnings.warn(
                            f"No memo found for {memo_name}: " f"this path must be new."
                        )
                        return propagate()

                    if not memo.caching_policy == caching_policy:
                        warnings.warn(
                            f"stored memo has caching policy {memo.caching_policy} while "
                            f"the method is decorated with {caching_policy}. "
                            f"The database must have been generated by an outdated version of "
                            f"memo-tools. Falling back to stored memo "
                            f"policy: ({memo.caching_policy})..."
                        )
                        strict_caching = (
                            _check_caching_policy(memo.caching_policy) == "strict"
                        )

                    # we serialize args and kwargs to compare them with the memoed ones
                    fn_args_kwargs = _dump((memo_args, kwargs))

                    if strict_caching:
                        # in strict mode, fn might return different results every time it is called --
                        # regardless of the arguments it is called with. So each memo contains a sequence of values,
                        # and a cursor to keep track of which one is next in the replay routine.
                        try:
                            current_cursor = memo.cursor
                            recording = memo.calls[current_cursor]
                            memo.cursor += 1
                        except IndexError:
                            # There is a memo, but its cursor is out of bounds.
                            # this means the current path is calling the wrapped function
                            # more times than the recorded path did.
                            # if this happens while replaying locally, of course, game over.
                            warnings.warn(
                                f"Memo cursor {current_cursor} out of bounds for {memo_name}: "
                                f"this path must have diverged. Propagating call..."
                            )
                            return propagate()

                        recorded_args_kwargs, recorded_output = recording

                        if recorded_args_kwargs != fn_args_kwargs:
                            # if this happens while replaying locally, of course, game over.
                            warnings.warn(
                                f"memoized {memo_name} arguments ({recorded_args_kwargs}) "
                                f"don't match the ones received at runtime ({fn_args_kwargs}). "
                                f"This path has diverged. Propagating call..."
                            )
                            return propagate()

                        if log_on_replay:
                            _log_memo(fn, args, kwargs, recorded_output, cache_hit=True)

                        return _load(recorded_output)  # happy path! good for you, path.

                    else:
                        # in non-strict mode, we don't care about the order in which fn is called:
                        #  it will return values in function of the arguments it is called with,
                        #  regardless of when it is called.
                        # so all we have to check is whether the arguments are known.
                        #  in non-strict mode, memo.calls is an inputs/output dict.
                        recorded_output = memo.calls.get(fn_args_kwargs, _NotFound)
                        if recorded_output is not _NotFound:
                            return _load(recorded_output)  # happy path! good for you, path.

                        warnings.warn(
                            f"No memo for {memo_name} matches the arguments received at runtime. "
                            f"This path has diverged."
                        )
                        return propagate()

                else:
                    msg = f"invalid memo mode: {_MEMO_MODE}"
                    warnings.warn(msg)
                    raise ValueError(msg)

            raise RuntimeError('Unhandled memo path.')

        return wrapper

    return decorator


class DB:
    def __init__(self, file: Path) -> None:
        self._file = file
        self.data = None

    def load(self):
        text = self._file.read_text()
        if not text:
            logger.debug("database empty; initializing with data=[]")
            self.data = Data([])
            return

        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            raise ValueError(f"database invalid: could not json-decode {self._file}")

        try:
            scenes = [Scene.from_dict(obj) for obj in raw.get("scenes", ())]
        except Exception as e:
            raise RuntimeError(
                f"database invalid: could not parse Scenes from {raw['scenes']!r}..."
            ) from e

        self.data = Data(scenes)

    def commit(self):
        self._file.write_text(json.dumps(asdict(self.data), indent=2))


@dataclass
class Event:
    env: Dict[str, str]
    timestamp: str  # datetime.datetime

    @property
    def name(self):
        return self.env["JUJU_DISPATCH_PATH"].split("/")[1]

    @property
    def datetime(self):
        return datetime.datetime.fromisoformat(self.timestamp)


@dataclass
class Memo:
    # todo clean this up by subclassing out to two separate StrictMemo and LooseMemo objects.
    # list of (args, kwargs), return-value pairs for this memo
    # warning: in reality it's all lists, no tuples.
    calls: Union[
        List[Tuple[str, Any]],  # if caching_policy == 'strict'
        Dict[str, Any],  # if caching_policy == 'loose'
    ] = field(default_factory=list)
    # indicates the position of the replay cursor if we're replaying the memo
    cursor: Union[
        int,  # if caching_policy == 'strict'
        Literal["n/a"],  # if caching_policy == 'loose'
    ] = 0
    caching_policy: _CachingPolicy = "strict"
    serializer: SUPPORTED_SERIALIZERS = "json"

    def __post_init__(self):
        if self.caching_policy == "loose" and not self.calls:  # first time only!
            self.calls = {}
            self.cursor = "n/a"

    def cache_call(self, input: str, output: str):
        assert isinstance(input, str), input
        assert isinstance(output, str), output

        if self.caching_policy == "loose":
            self.calls[input] = output
        else:
            self.calls.append((input, output))


@dataclass
class Context:
    memos: Dict[str, Memo] = field(default_factory=dict)

    @staticmethod
    def from_dict(obj: dict):
        return Context(
            memos={name: Memo(**content) for name, content in obj["memos"].items()}
        )


@dataclass
class Scene:
    event: Event
    context: Context = Context()

    @staticmethod
    def from_dict(obj):
        return Scene(
            event=Event(**obj["event"]),
            context=Context.from_dict(obj.get("context", {})),
        )


@dataclass
class Data:
    scenes: List[Scene]


@contextmanager
def event_db(file=DEFAULT_DB_NAME) -> Generator[Data, None, None]:
    path = Path(file)
    if not path.exists():
        print(f"Initializing DB file at {path}...")
        path.touch(mode=0o666)
        path.write_text("{}")  # empty json obj

    db = DB(file=path)
    db.load()
    yield db.data
    db.commit()


def _capture() -> Event:
    return Event(env=dict(os.environ), timestamp=datetime.datetime.now().isoformat())


def _reset_replay_cursors(file=DEFAULT_DB_NAME, *scene_idx: int):
    """Reset the replay cursor for all scenes, or the specified ones."""
    with event_db(file) as data:
        to_reset = (data.scenes[idx] for idx in scene_idx) if scene_idx else data.scenes
        for scene in to_reset:
            for memo in scene.context.memos.values():
                memo.cursor = 0


def _record_current_event(file) -> Event:
    with event_db(file) as data:
        scenes = data.scenes
        event = _capture()
        scenes.append(Scene(event=event))
    return event


def setup(file=DEFAULT_DB_NAME):
    _MEMO_MODE: MemoModes = _load_memo_mode()

    if _MEMO_MODE == "record":
        event = _record_current_event(file)
        print(f"Captured event: {event.name}.")

    if _MEMO_MODE == "replay":
        _reset_replay_cursors()
        print(f"Replaying: reset replay cursors.")
