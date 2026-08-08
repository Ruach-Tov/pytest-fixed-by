"""@opcounted -- exact bytecode-instruction counting decorator (Python 3.12+).

Deterministic complexity measurement: wall-clock varies with machine load and
cache weather, but the number of interpreted instructions a pure-Python
function executes is exact and repeatable -- same input, same count, any
machine.  Complexity laws read off as ratios between two runs (worst-case
bubble sort at n=20 vs n=40: ratio 3.997 against the quadratic prediction 4).

Wraps a function with sys.monitoring INSTRUCTION-event counting and writes a
JSON record per call:  opcount.<PID>.<method>.<timestamp>.json

Usage:
    from opcount import opcounted

    @opcounted
    def work(...): ...

    @opcounted(outdir="/tmp", warm=True)
    def hot(...): ...

Notes:
  * Counts INTERPRETED bytecode instructions -- C-level work (numpy, sorted,
    BDD libraries) is opaque to this measure. For mixed workloads, count
    domain operations instead.
  * Deterministic: same input -> same count (unlike wall-clock).
  * warm=True runs one uncounted call first so 3.12's adaptive-interpreter
    specialization settles before measurement (counts are 1:1 stable either
    way, but the first call can differ slightly).
  * Nested @opcounted functions: inner counts are included in the outer's
    total AND reported separately (each writes its own JSON).
  * Overhead: the counted call runs ~2-20x slower (callback per instruction).
    The count itself is exact regardless.
"""
import sys, os, json, time, functools, threading

_mon = sys.monitoring
_TOOL_NAME = "opcount"
_lock = threading.Lock()
_tool_id = None
_counter_stack = []      # for nested counted calls


def _acquire_tool():
    global _tool_id
    if _tool_id is not None:
        return _tool_id
    with _lock:
        if _tool_id is not None:
            return _tool_id
        for tid in range(6):
            try:
                _mon.use_tool_id(tid, _TOOL_NAME)
                _tool_id = tid
                break
            except ValueError:
                continue
        if _tool_id is None:
            raise RuntimeError("opcount: no free sys.monitoring tool id")
        _mon.register_callback(_tool_id, _mon.events.INSTRUCTION, _on_instruction)
    return _tool_id


def _on_instruction(code, offset):
    for c in _counter_stack:
        c[0] += 1


def opcounted(fn=None, *, outdir=None, warm=False, write_json=True):
    """decorator: count interpreted bytecode instructions per call, write JSON."""
    def wrap(f):
        state = {"warmed": not warm}

        @functools.wraps(f)
        def inner(*args, **kwargs):
            tid = _acquire_tool()
            if not state["warmed"]:
                state["warmed"] = True
                f(*args, **kwargs)          # uncounted warm-up call
            box = [0]
            _counter_stack.append(box)
            outermost = len(_counter_stack) == 1
            if outermost:
                _mon.set_events(tid, _mon.events.INSTRUCTION)
            t0 = time.time()
            try:
                result = f(*args, **kwargs)
            finally:
                wall = time.time() - t0
                if outermost:
                    _mon.set_events(tid, 0)
                _counter_stack.pop()
                if write_json:
                    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) \
                         + f"{time.time() % 1:.6f}"[1:]
                    rec = {
                        "method": f.__qualname__,
                        "module": f.__module__,
                        "instructions": box[0],
                        "wall_seconds": round(wall, 6),
                        "pid": os.getpid(),
                        "timestamp_utc": ts,
                        "python": sys.version.split()[0],
                        "nested_within_another_opcounted": not outermost,
                        "args_repr": repr(args)[:200],
                        "kwargs_repr": repr(kwargs)[:200],
                    }
                    name = f"opcount.{os.getpid()}.{f.__name__}.{ts}.json"
                    path = os.path.join(outdir or os.getcwd(), name)
                    try:
                        with open(path, "w") as fh:
                            json.dump(rec, fh, indent=1)
                    except OSError:
                        pass
            return result
        return inner
    return wrap(fn) if fn is not None else wrap
